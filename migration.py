"""历史数据平滑迁移：老库（archive_files/copy_files 自带 content BLOB）
迁到按摘要共用的 content_blobs 结构。

迁移是幂等的：已迁过的库（V2）什么都不做。迁移完成后老版本清单、
副本校验修复、格式迁移全部照常可用——引用关系和元数据完整保留。
"""
from __future__ import annotations

import sqlite3

from db import SCHEMA_VERSION, column_exists, create_core_tables, exec_ddl, now, table_exists
from cleanup import create_cleanup_tables


def needs_legacy_migration(conn: sqlite3.Connection) -> bool:
    """老库特征：版本文件表存在、内容实体表不存在、文件表内联 content 列。"""
    if not table_exists(conn, "archive_files"):
        return False
    if table_exists(conn, "content_blobs"):
        return False
    return column_exists(conn, "archive_files", "content")


def _backfill_blobs(conn: sqlite3.Connection) -> int:
    """从两张老文件表去重抽取内容实体，返回实体数量。"""
    rows = conn.execute(
        """
        SELECT sha256, MAX(size) AS size, MAX(content) AS content
        FROM (
            SELECT sha256, size, content FROM archive_files
            UNION ALL
            SELECT sha256, size, content FROM copy_files
        )
        GROUP BY sha256
        """
    ).fetchall()
    conn.executemany(
        "INSERT INTO content_blobs(sha256,size,content,created_at) VALUES(?,?,?,?)",
        [(r["sha256"], r["size"], r["content"], now()) for r in rows],
    )
    return len(rows)


_ARCHIVE_FILES_DDL = """
CREATE TABLE archive_files_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
    UNIQUE(version_id,path)
);
INSERT INTO archive_files_v2(id,version_id,path,sha256,size,blob_id)
SELECT f.id,f.version_id,f.path,f.sha256,f.size,b.id
FROM archive_files f JOIN content_blobs b ON b.sha256=f.sha256;
DROP TABLE archive_files;
ALTER TABLE archive_files_v2 RENAME TO archive_files;
"""

_COPY_FILES_DDL = """
CREATE TABLE copy_files_v2(
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
    PRIMARY KEY(copy_id,path)
);
INSERT INTO copy_files_v2(copy_id,path,sha256,size,blob_id)
SELECT f.copy_id,f.path,f.sha256,f.size,b.id
FROM copy_files f JOIN content_blobs b ON b.sha256=f.sha256;
DROP TABLE copy_files;
ALTER TABLE copy_files_v2 RENAME TO copy_files;
"""

_AUDIT_DDL = """
CREATE TABLE audit_log_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER REFERENCES archives(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO audit_log_v2(id,archive_id,actor_id,action,detail,created_at)
SELECT id,archive_id,actor_id,action,detail,created_at FROM audit_log;
DROP TABLE audit_log;
ALTER TABLE audit_log_v2 RENAME TO audit_log;
"""


def migrate_legacy(conn: sqlite3.Connection) -> dict | None:
    """执行 V1 -> V2 迁移。返回统计；无需迁移时返回 None。

    调用方负责在外层关闭 foreign_keys（DROP/RENAME 重建表期间避免
    外键拦截），整个迁移在单个事务里完成，失败整体回滚。
    """
    if not needs_legacy_migration(conn):
        return None
    conn.execute("BEGIN")
    try:
        # CORE DDL 对所有老表都是 IF NOT EXISTS，只补上缺失的 content_blobs
        # 与 schema_migrations，不动既有数据。
        create_core_tables(conn)
        blob_count = _backfill_blobs(conn)
        version_files = conn.execute("SELECT COUNT(*) AS n FROM archive_files").fetchone()["n"]
        copy_files = conn.execute("SELECT COUNT(*) AS n FROM copy_files").fetchone()["n"]
        exec_ddl(conn, _ARCHIVE_FILES_DDL)
        exec_ddl(conn, _COPY_FILES_DDL)
        if table_exists(conn, "audit_log"):
            # 老定义 archive_id NOT NULL；整理任务要写全局审计行，对齐 V2 定义。
            exec_ddl(conn, _AUDIT_DDL)
        create_cleanup_tables(conn)
        conn.commit()
        return {
            "from_version": 1,
            "to_version": SCHEMA_VERSION,
            "content_blobs": blob_count,
            "archive_files": version_files,
            "copy_files": copy_files,
        }
    except Exception:
        conn.rollback()
        raise
