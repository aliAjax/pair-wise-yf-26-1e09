"""存储整理任务：找出无人引用的残留内容实体，管理员确认后清掉。

安全规则：
- “没人引用”以同一时刻两张引用表（archive_files / copy_files）为准；
- 清理与新提交、新登记副本、迁移都在同一个 IMMEDIATE 写事务里串行执行，
  对方事务一旦登记引用就可见，被引用的实体不会被误清空；
- 先查看（dry-run 预览）后确认删除，删除动作落 storage_gc_jobs 任务记录。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# 残留实体：版本清单和副本两边都没有引用。
ORPHAN_FILTER = """
    WHERE NOT EXISTS (SELECT 1 FROM archive_files af WHERE af.sha256 = b.sha256)
      AND NOT EXISTS (SELECT 1 FROM copy_files cf WHERE cf.sha256 = b.sha256)
"""

ORPHAN_DELETE = """
    DELETE FROM content_blobs
    WHERE NOT EXISTS (SELECT 1 FROM archive_files af WHERE af.sha256 = content_blobs.sha256)
      AND NOT EXISTS (SELECT 1 FROM copy_files cf WHERE cf.sha256 = content_blobs.sha256)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entity_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    count, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM content_blobs"
    ).fetchone()
    return count, total


def _reference_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    count, total = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(size),0) FROM (
               SELECT size FROM archive_files UNION ALL SELECT size FROM copy_files
           )"""
    ).fetchone()
    return count, total


def _orphan_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    count, total = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM content_blobs b {ORPHAN_FILTER}"
    ).fetchone()
    return count, total


def list_orphans(conn: sqlite3.Connection, limit: int = 100) -> dict:
    """预览残留实体和可回收空间，不做任何删除。"""
    orphan_count, orphan_bytes = _orphan_totals(conn)
    rows = conn.execute(
        f"""SELECT b.sha256, b.size, b.created_at FROM content_blobs b {ORPHAN_FILTER}
            ORDER BY b.size DESC, b.sha256 LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "orphan_count": orphan_count,
        "orphan_bytes": orphan_bytes,
        "limit": limit,
        "items": [{"sha256": r["sha256"], "size": r["size"], "created_at": r["created_at"]} for r in rows],
    }


def storage_overview(conn: sqlite3.Connection, job_limit: int = 10) -> dict:
    """整理存储页总览：物理占用、逻辑引用、可回收空间和历史整理任务。"""
    entity_count, entity_bytes = _entity_totals(conn)
    ref_count, logical_bytes = _reference_totals(conn)
    orphan_count, orphan_bytes = _orphan_totals(conn)
    jobs = conn.execute(
        "SELECT * FROM storage_gc_jobs ORDER BY id DESC LIMIT ?", (job_limit,)
    ).fetchall()
    return {
        "entity_count": entity_count,
        "entity_bytes": entity_bytes,
        "reference_count": ref_count,
        "logical_bytes": logical_bytes,
        "shared_saved_bytes": max(0, logical_bytes - entity_bytes),
        "orphan_count": orphan_count,
        "orphan_bytes": orphan_bytes,
        "jobs": [dict(j) for j in jobs],
    }


def purge_orphans(conn: sqlite3.Connection, actor_id: str) -> dict:
    """管理员确认后清空残留实体。整段在 IMMEDIATE 事务内，保证与新提交互斥。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        before_count, before_bytes = _orphan_totals(conn)
        cur = conn.execute(ORPHAN_DELETE)
        deleted_count = cur.rowcount
        after_count, after_bytes = _orphan_totals(conn)
        deleted_bytes = before_bytes - after_bytes
        job_cur = conn.execute(
            """INSERT INTO storage_gc_jobs(actor_id,orphan_count_before,orphan_bytes_before,
               deleted_count,deleted_bytes,created_at) VALUES(?,?,?,?,?,?)""",
            (actor_id, before_count, before_bytes, deleted_count, deleted_bytes, _now()),
        )
        job = conn.execute("SELECT * FROM storage_gc_jobs WHERE id=?", (job_cur.lastrowid,)).fetchone()
        return {
            "job": dict(job),
            "orphan_count_before": before_count,
            "orphan_bytes_before": before_bytes,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "orphan_count_after": after_count,
            "orphan_bytes_after": after_bytes,
        }
    except Exception:
        conn.rollback()
        raise
