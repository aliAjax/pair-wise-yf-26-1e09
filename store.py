"""档案保存业务层。

版本文件与副本文件的内容不再各自落盘，统一引用 content_blobs 内容实体
（规则见 content.py）。副本"介质损坏"通过写时复制引用表达：损坏时把该
副本对应路径的引用改指到损坏实体，绝不修改共享实体；修复时引用指回
健康实体。存储整理任务见 cleanup.py。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

from db import DEFAULT_DB, BusinessError, connect, create_core_tables, now, write_audit
from content import intern_blob, intern_file, storage_overview, verify_manifest
from cleanup import (
    cancel_cleanup,
    confirm_cleanup,
    create_cleanup_tables,
    get_task,
    list_tasks,
    plan_cleanup,
)
from migration import migrate_legacy, needs_legacy_migration

MANAGERS = {"owner", "archivist"}
READERS = MANAGERS | {"auditor"}


class PreservationStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.last_migration: dict | None = None

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def init_schema(self) -> dict | None:
        """建表；检测到老结构时平滑迁移。返回迁移统计（新库/已迁移返回 None）。"""
        with self._lock:
            conn = self.connect()
            try:
                if needs_legacy_migration(conn):
                    # 重建表期间关闭外键（PRAGMA 不能在事务内执行）。
                    conn.execute("PRAGMA foreign_keys=OFF")
                    stats = migrate_legacy(conn)
                    conn.execute("PRAGMA foreign_keys=ON")
                    self.last_migration = stats
                    return stats
                conn.execute("BEGIN")
                try:
                    create_core_tables(conn)
                    create_cleanup_tables(conn)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return None
            finally:
                conn.close()

    def seed(self) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("owner", "机构档案负责人", "owner"),
                    ("archivist", "档案管理员", "archivist"),
                    ("auditor", "独立审计员", "auditor"),
                    ("outsider", "未授权访客", "auditor"),
                ],
            )

    def _user(self, conn, user_id: str | None, roles: set[str] | None = None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _access(self, conn, archive_id: int, user: sqlite3.Row, require_write: bool = False) -> None:
        archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            raise BusinessError("档案不存在", 404, "not_found")
        if archive["owner_id"] == user["id"]:
            return
        row = conn.execute(
            "SELECT permission FROM archive_members WHERE archive_id=? AND user_id=?", (archive_id, user["id"])
        ).fetchone()
        if not row or (require_write and row["permission"] != "write"):
            raise BusinessError("没有该受限档案的访问权限", 403, "forbidden")

    def _audit(self, conn, archive_id: int | None, actor: str, action: str, detail: dict) -> None:
        write_audit(conn, archive_id, actor, action, detail)

    # ---- 档案与授权 -------------------------------------------------------

    def create_archive(self, user_id: str, name: str, retention_until: str, restricted: bool = True) -> dict:
        name = name.strip()
        if len(name) < 2:
            raise BusinessError("档案名称至少 2 字", 422, "invalid_name")
        try:
            deadline = date.fromisoformat(retention_until)
        except ValueError:
            raise BusinessError("retention_until 必须是 YYYY-MM-DD", 422, "invalid_retention")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "retention_in_past")
        with self.connect() as conn:
            self._user(conn, user_id, MANAGERS)
            try:
                cur = conn.execute(
                    "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,?,?)",
                    (name, user_id, retention_until, int(bool(restricted)), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("档案名称已存在", 409, "archive_exists")
            archive_id = cur.lastrowid
            conn.execute(
                "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')", (archive_id, user_id)
            )
            self._audit(conn, archive_id, user_id, "archive.create",
                        {"retention_until": retention_until, "restricted": restricted})
            return {"id": archive_id, "name": name, "retention_until": retention_until, "restricted": restricted}

    def grant(self, actor_id: str, archive_id: int, user_id: str, permission: str) -> dict:
        if permission not in {"read", "write"}:
            raise BusinessError("permission 必须是 read 或 write", 422, "invalid_permission")
        with self.connect() as conn:
            actor = self._user(conn, actor_id)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            if not archive:
                raise BusinessError("档案不存在", 404, "not_found")
            if archive["owner_id"] != actor_id:
                raise BusinessError("只有档案所有者可以授权", 403, "forbidden")
            self._user(conn, user_id)
            conn.execute(
                """INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,?)
                   ON CONFLICT(archive_id,user_id) DO UPDATE SET permission=excluded.permission""",
                (archive_id, user_id, permission),
            )
            self._audit(conn, archive_id, actor_id, "access.grant", {"user_id": user_id, "permission": permission})
            return {"archive_id": archive_id, "user_id": user_id, "permission": permission}

    # ---- 版本提交：内容按摘要共用 ----------------------------------------

    def ingest_version(self, actor_id: str, archive_id: int, files: object) -> dict:
        manifest = verify_manifest(files)
        with self.connect() as conn:
            actor = self._user(conn, actor_id, MANAGERS)
            self._access(conn, archive_id, actor, require_write=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?", (archive_id,)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (archive_id, version_no, actor_id, now()),
                )
                version_id = cur.lastrowid
                shared = 0
                for item in manifest:
                    before = conn.execute("SELECT COUNT(*) FROM content_blobs WHERE sha256=?", (item["sha256"],)).fetchone()[0]
                    intern_file(conn, "archive_files", ("version_id",), (version_id,),
                                item["path"], item["sha256"], item["size"], item["content"])
                    shared += before
                self._audit(
                    conn, archive_id, actor_id, "version.ingest",
                    {"version_id": version_id, "version": version_no, "files": len(manifest),
                     "shared_blobs": shared,
                     "manifest": [{"path": x["path"], "sha256": x["sha256"], "size": x["size"]} for x in manifest]},
                )
                return {"id": version_id, "archive_id": archive_id, "version": version_no,
                        "file_count": len(manifest), "shared_blobs": shared}
            except Exception:
                conn.rollback()
                raise

    # ---- 副本登记：只登记引用，不复制实体 --------------------------------

    def add_copy(self, actor_id: str, version_id: int, location: str) -> dict:
        location = location.strip()
        if len(location) < 2:
            raise BusinessError("副本位置不能为空", 422, "invalid_location")
        with self.connect() as conn:
            actor = self._user(conn, actor_id, MANAGERS)
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], actor, require_write=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO copies(version_id,location,created_at,last_verified_at) VALUES(?,?,?,?)",
                    (version_id, location, now(), now()),
                )
                copy_id = cur.lastrowid
                # 直接引用版本清单的内容实体：同摘要天然共用，不复制任何字节。
                conn.execute(
                    """INSERT INTO copy_files(copy_id,path,sha256,size,blob_id)
                       SELECT ?,path,sha256,size,blob_id FROM archive_files WHERE version_id=?""",
                    (copy_id, version_id),
                )
                self._audit(conn, version["archive_id"], actor_id, "copy.create",
                            {"copy_id": copy_id, "version_id": version_id, "location": location})
                return {"id": copy_id, "version_id": version_id, "location": location, "state": "healthy"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该版本的副本位置已存在", 409, "copy_exists")
            except Exception:
                conn.rollback()
                raise

    def get_version(self, user_id: str, version_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, READERS)
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], user)
            files = conn.execute(
                "SELECT path,sha256,size,blob_id FROM archive_files WHERE version_id=? ORDER BY path", (version_id,)
            ).fetchall()
            copies = conn.execute(
                "SELECT id,location,state,last_verified_at FROM copies WHERE version_id=? ORDER BY id", (version_id,)
            ).fetchall()
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (version["archive_id"],)).fetchone()
            return {"version": dict(version), "archive": dict(archive),
                    "files": [dict(x) for x in files], "copies": [dict(x) for x in copies]}

    # ---- 副本校验与修复：损坏写时复制，修复重指引用 ----------------------

    def verify_copy(self, user_id: str, copy_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, READERS)
            try:
                conn.execute("BEGIN IMMEDIATE")
                copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
                if not copy:
                    raise BusinessError("副本不存在", 404, "not_found")
                version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
                self._access(conn, version["archive_id"], user)
                stored = conn.execute(
                    """SELECT f.path,f.sha256,f.size,f.blob_id,b.content
                       FROM copy_files f JOIN content_blobs b ON b.id=f.blob_id
                       WHERE f.copy_id=? ORDER BY f.path""",
                    (copy_id,),
                ).fetchall()
                corrupt_paths = [
                    r["path"] for r in stored
                    if hashlib.sha256(r["content"]).hexdigest() != r["sha256"] or len(r["content"]) != r["size"]
                ]
                repaired = False
                if not corrupt_paths:
                    conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?",
                                 (now(), copy_id))
                    result_state = "healthy"
                else:
                    conn.execute("UPDATE copies SET state='corrupt',last_verified_at=? WHERE id=?",
                                 (now(), copy_id))
                    healthy = conn.execute(
                        "SELECT id FROM copies WHERE version_id=? AND id<>? AND state='healthy' ORDER BY last_verified_at DESC LIMIT 1",
                        (copy["version_id"], copy_id),
                    ).fetchone()
                    result_state = "degraded"
                    if healthy:
                        donor = conn.execute(
                            """SELECT f.path,f.sha256,f.size,f.blob_id,b.content
                               FROM copy_files f JOIN content_blobs b ON b.id=f.blob_id
                               WHERE f.copy_id=? ORDER BY f.path""",
                            (healthy["id"],),
                        ).fetchall()
                        donor_by_path = {r["path"]: r for r in donor}
                        expected = {r["path"]: r for r in conn.execute(
                            "SELECT path,sha256,size,blob_id FROM archive_files WHERE version_id=?",
                            (copy["version_id"],),
                        ).fetchall()}
                        if set(donor_by_path) == set(expected) and all(
                            hashlib.sha256(donor_by_path[p]["content"]).hexdigest() == expected[p]["sha256"]
                            for p in expected
                        ):
                            # 不删行重插，只把损坏路径的引用指回健康实体，
                            # 快照哈希/大小同步恢复。
                            for path in corrupt_paths:
                                conn.execute(
                                    """UPDATE copy_files
                                       SET blob_id=?,sha256=?,size=?
                                       WHERE copy_id=? AND path=?""",
                                    (donor_by_path[path]["blob_id"], expected[path]["sha256"],
                                     expected[path]["size"], copy_id, path),
                                )
                            conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?",
                                         (now(), copy_id))
                            repaired, result_state = True, "healthy"
                    if result_state == "degraded":
                        conn.execute("UPDATE archive_versions SET state='degraded' WHERE id=?", (copy["version_id"],))
                self._audit(
                    conn, version["archive_id"], user_id, "copy.verify",
                    {"copy_id": copy_id, "state": result_state, "corrupt_paths": corrupt_paths, "repaired": repaired},
                )
                return {"copy_id": copy_id, "state": result_state,
                        "corrupt_paths": corrupt_paths, "repaired": repaired}
            except Exception:
                conn.rollback()
                raise

    def simulate_corruption(self, user_id: str, copy_id: int, path: str) -> dict:
        """仅用于演示和测试，在受控环境中模拟底层介质损坏。

        共享存储下不能改实体本身（会连累所有引用），改为写时复制：
        生成损坏内容实体，把该副本该路径的引用改指过去。
        """
        with self.connect() as conn:
            user = self._user(conn, user_id, MANAGERS)
            try:
                conn.execute("BEGIN IMMEDIATE")
                copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
                if not copy:
                    raise BusinessError("副本不存在", 404, "not_found")
                version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
                self._access(conn, version["archive_id"], user, require_write=True)
                row = conn.execute(
                    """SELECT b.content FROM copy_files f JOIN content_blobs b ON b.id=f.blob_id
                       WHERE f.copy_id=? AND f.path=?""",
                    (copy_id, path),
                ).fetchone()
                if not row:
                    raise BusinessError("副本文件不存在", 404, "not_found")
                damaged = bytes([row["content"][0] ^ 0xFF]) + row["content"][1:] if row["content"] else b"corrupt"
                # 损坏实体独立存放；快照 sha256 列保留期望值，校验时据此发现损坏。
                damaged_blob = intern_blob(conn, damaged)
                conn.execute("UPDATE copy_files SET blob_id=? WHERE copy_id=? AND path=?", (damaged_blob, copy_id, path))
                conn.execute("UPDATE copies SET state='corrupt' WHERE id=?", (copy_id,))
                self._audit(conn, version["archive_id"], user_id, "copy.simulate_corruption",
                            {"copy_id": copy_id, "path": path})
                return {"copy_id": copy_id, "path": path, "state": "corrupt"}
            except Exception:
                conn.rollback()
                raise

    # ---- 格式迁移：未变文件沿用引用，新文件按摘要共用 --------------------

    def migrate(self, actor_id: str, version_id: int, source_path: str, target_path: str,
                target_format: str, content_b64: str) -> dict:
        with self.connect() as conn:
            actor = self._user(conn, actor_id, MANAGERS)
            source_version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not source_version:
                raise BusinessError("源档案版本不存在", 404, "not_found")
            self._access(conn, source_version["archive_id"], actor, require_write=True)
            source = conn.execute(
                "SELECT * FROM archive_files WHERE version_id=? AND path=?", (version_id, source_path)
            ).fetchone()
            if not source:
                raise BusinessError("源文件不存在", 404, "source_not_found")
            converted = verify_manifest([{"path": target_path, "content_b64": content_b64}])[0]
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?",
                    (source_version["archive_id"],),
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (source_version["archive_id"], version_no, actor_id, now()),
                )
                target_version_id = cur.lastrowid
                # 未参与转换的文件连 blob_id 一起沿用，零拷贝共享实体。
                conn.execute(
                    """INSERT INTO archive_files(version_id,path,sha256,size,blob_id)
                       SELECT ?,path,sha256,size,blob_id FROM archive_files
                       WHERE version_id=? AND path<>?""",
                    (target_version_id, version_id, source_path),
                )
                intern_file(conn, "archive_files", ("version_id",), (target_version_id,),
                            converted["path"], converted["sha256"], converted["size"], converted["content"])
                conn.execute(
                    "INSERT INTO migrations(source_version_id,target_version_id,source_path,target_path,target_format,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, target_version_id, source_path, converted["path"],
                     target_format.strip(), actor_id, now()),
                )
                self._audit(
                    conn, source_version["archive_id"], actor_id, "format.migrate",
                    {"source_version_id": version_id, "target_version_id": target_version_id,
                     "source_path": source_path, "target_path": converted["path"],
                     "target_format": target_format.strip()},
                )
                return {"id": target_version_id, "version": version_no,
                        "source_version_id": version_id, "target_path": converted["path"]}
            except Exception:
                conn.rollback()
                raise

    def archive_status(self, user_id: str, archive_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, READERS)
            self._access(conn, archive_id, user)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            versions = conn.execute(
                "SELECT id,version,state,created_at FROM archive_versions WHERE archive_id=? ORDER BY version",
                (archive_id,),
            ).fetchall()
            deadline = date.fromisoformat(archive["retention_until"])
            return {
                "archive": dict(archive),
                "days_remaining": (deadline - date.today()).days,
                "versions": [dict(v) | {
                    "file_count": conn.execute("SELECT COUNT(*) FROM archive_files WHERE version_id=?", (v["id"],)).fetchone()[0],
                    "copy_count": conn.execute("SELECT COUNT(*) FROM copies WHERE version_id=?", (v["id"],)).fetchone()[0],
                } for v in versions],
                "audit": [dict(r) | {"detail": json.loads(r["detail"])}
                          for r in conn.execute("SELECT * FROM audit_log WHERE archive_id=? ORDER BY id", (archive_id,)).fetchall()],
            }

    # ---- 存储整理 ---------------------------------------------------------

    def storage_status(self, user_id: str) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, READERS)
            return storage_overview(conn)

    def create_cleanup_plan(self, user_id: str) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, MANAGERS)
            return plan_cleanup(conn, user_id)

    def confirm_cleanup_task(self, user_id: str, task_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, MANAGERS)
            return confirm_cleanup(conn, user_id, task_id)

    def cancel_cleanup_task(self, user_id: str, task_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, MANAGERS)
            return cancel_cleanup(conn, user_id, task_id)

    def get_cleanup_task(self, user_id: str, task_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, READERS)
            return get_task(conn, task_id)

    def list_cleanup_tasks(self, user_id: str) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, READERS)
            return {"tasks": list_tasks(conn)}
