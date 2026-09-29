"""存储整理任务：两阶段清理没有引用的残留内容实体。

第一阶段 plan：快照当前所有残留实体（占用空间），交给管理员核对。
第二阶段 confirm：在写事务内重新核对引用后才删除；被引用的实体
即使出现在旧快照里也跳过，content_blobs 的外键约束再兜底，保证
整理与新提交/副本修复并发时不会清出空文件。
"""
from __future__ import annotations

import sqlite3

from db import BusinessError, exec_ddl, now, write_audit
from content import orphan_blobs

CLEANUP_DDL = """
CREATE TABLE IF NOT EXISTS cleanup_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','confirmed','cancelled')),
    blob_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS cleanup_task_blobs(
    task_id INTEGER NOT NULL,
    blob_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','deleted','skipped_referenced','skipped_missing')),
    PRIMARY KEY(task_id,blob_id)
);
"""


def create_cleanup_tables(conn: sqlite3.Connection) -> None:
    exec_ddl(conn, CLEANUP_DDL)


def plan_cleanup(conn: sqlite3.Connection, actor_id: str) -> dict:
    """生成整理计划。空计划直接报错，避免管理员对着空任务点确认。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        orphans = orphan_blobs(conn)
        if not orphans:
            raise BusinessError("没有可清理的残留内容实体", 409, "nothing_to_clean")
        cur = conn.execute(
            "INSERT INTO cleanup_tasks(state,blob_count,total_bytes,created_by,created_at) VALUES('planned',?,?,?,?)",
            (len(orphans), sum(r["size"] for r in orphans), actor_id, now()),
        )
        task_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO cleanup_task_blobs(task_id,blob_id,sha256,size,state) VALUES(?,?,?,?,'pending')",
            [(task_id, r["id"], r["sha256"], r["size"]) for r in orphans],
        )
        write_audit(
            conn, None, actor_id, "storage.cleanup.plan",
            {"task_id": task_id, "blob_count": len(orphans), "total_bytes": sum(r["size"] for r in orphans)},
        )
        return get_task(conn, task_id)
    except Exception:
        conn.rollback()
        raise


def _is_referenced(conn: sqlite3.Connection, blob_id: int) -> bool:
    return conn.execute(
        """SELECT 1 WHERE EXISTS(SELECT 1 FROM archive_files WHERE blob_id=?)
                 OR EXISTS(SELECT 1 FROM copy_files WHERE blob_id=?)""",
        (blob_id, blob_id),
    ).fetchone() is not None


def confirm_cleanup(conn: sqlite3.Connection, actor_id: str, task_id: int) -> dict:
    """确认执行整理：逐条重新核对引用，删除仍无引用的实体。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM cleanup_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise BusinessError("整理任务不存在", 404, "not_found")
        if task["state"] != "planned":
            raise BusinessError("整理任务已处理，不能重复确认", 409, "task_closed")
        items = conn.execute("SELECT * FROM cleanup_task_blobs WHERE task_id=? ORDER BY blob_id", (task_id,)).fetchall()
        deleted, skipped, freed = [], [], 0
        for item in items:
            blob_id = item["blob_id"]
            still_exists = conn.execute("SELECT 1 FROM content_blobs WHERE id=?", (blob_id,)).fetchone()
            if not still_exists:
                conn.execute("UPDATE cleanup_task_blobs SET state='skipped_missing' WHERE task_id=? AND blob_id=?", (task_id, blob_id))
                skipped.append({"blob_id": blob_id, "reason": "missing"})
                continue
            # 计划生成之后若被新版本/副本/修复引用，绝不删除（外键约束还会再兜底）。
            if _is_referenced(conn, blob_id):
                conn.execute(
                    "UPDATE cleanup_task_blobs SET state='skipped_referenced' WHERE task_id=? AND blob_id=?",
                    (task_id, blob_id),
                )
                skipped.append({"blob_id": blob_id, "reason": "referenced"})
                continue
            conn.execute("DELETE FROM content_blobs WHERE id=?", (blob_id,))
            conn.execute("UPDATE cleanup_task_blobs SET state='deleted' WHERE task_id=? AND blob_id=?", (task_id, blob_id))
            deleted.append({"blob_id": blob_id, "sha256": item["sha256"], "size": item["size"]})
            freed += item["size"]
        conn.execute("UPDATE cleanup_tasks SET state='confirmed',confirmed_at=? WHERE id=?", (now(), task_id))
        write_audit(
            conn, None, actor_id, "storage.cleanup.confirm",
            {"task_id": task_id, "deleted": len(deleted), "skipped": len(skipped), "freed_bytes": freed},
        )
        return get_task(conn, task_id)
    except Exception:
        conn.rollback()
        raise


def cancel_cleanup(conn: sqlite3.Connection, actor_id: str, task_id: int) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM cleanup_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise BusinessError("整理任务不存在", 404, "not_found")
        if task["state"] != "planned":
            raise BusinessError("整理任务已处理，不能取消", 409, "task_closed")
        conn.execute("UPDATE cleanup_tasks SET state='cancelled',confirmed_at=? WHERE id=?", (now(), task_id))
        write_audit(conn, None, actor_id, "storage.cleanup.cancel", {"task_id": task_id})
        return get_task(conn, task_id)
    except Exception:
        conn.rollback()
        raise


def get_task(conn: sqlite3.Connection, task_id: int) -> dict:
    task = conn.execute("SELECT * FROM cleanup_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise BusinessError("整理任务不存在", 404, "not_found")
    items = conn.execute("SELECT * FROM cleanup_task_blobs WHERE task_id=? ORDER BY blob_id", (task_id,)).fetchall()
    deleted = [dict(b) for b in items if b["state"] == "deleted"]
    return {
        "id": task["id"],
        "state": task["state"],
        "created_by": task["created_by"],
        "created_at": task["created_at"],
        "confirmed_at": task["confirmed_at"],
        "planned_blob_count": task["blob_count"],
        "planned_total_bytes": task["total_bytes"],
        "deleted_blob_count": len(deleted),
        "freed_bytes": sum(b["size"] for b in deleted),
        "items": [dict(b) for b in items],
    }


def list_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id FROM cleanup_tasks ORDER BY id DESC").fetchall()
    return [get_task(conn, r["id"]) for r in rows]
