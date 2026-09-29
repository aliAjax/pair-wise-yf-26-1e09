"""数据表层：内容寻址实体表、引用表结构，以及历史库到新结构的平滑迁移。

新结构下文件字节只存一份实体（content_blobs，按 SHA-256 摘要寻址），
版本清单（archive_files）和副本（copy_files）都只登记引用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CONTENT_BLOBS_DDL = """
CREATE TABLE IF NOT EXISTS content_blobs(
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK(size >= 0),
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""

ARCHIVE_FILES_DDL = """
CREATE TABLE IF NOT EXISTS archive_files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    size INTEGER NOT NULL CHECK(size >= 0),
    UNIQUE(version_id,path)
);
"""

COPY_FILES_DDL = """
CREATE TABLE IF NOT EXISTS copy_files(
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    size INTEGER NOT NULL CHECK(size >= 0),
    PRIMARY KEY(copy_id,path)
);
"""

GC_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS storage_gc_jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES users(id),
    orphan_count_before INTEGER NOT NULL,
    orphan_bytes_before INTEGER NOT NULL,
    deleted_count INTEGER NOT NULL,
    deleted_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor'))
);
CREATE TABLE IF NOT EXISTS archives(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL REFERENCES users(id),
    retention_until TEXT NOT NULL,
    restricted INTEGER NOT NULL DEFAULT 1 CHECK(restricted IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_members(
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    permission TEXT NOT NULL CHECK(permission IN ('read','write')),
    PRIMARY KEY(archive_id,user_id)
);
CREATE TABLE IF NOT EXISTS archive_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'verified' CHECK(state IN ('verified','degraded')),
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(archive_id,version)
);
CREATE TABLE IF NOT EXISTS copies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    location TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','degraded')),
    created_at TEXT NOT NULL,
    last_verified_at TEXT,
    UNIQUE(version_id,location)
);
CREATE TABLE IF NOT EXISTS migrations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    target_version_id INTEGER NOT NULL UNIQUE REFERENCES archive_versions(id),
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_format TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""" + CONTENT_BLOBS_DDL + ARCHIVE_FILES_DDL + COPY_FILES_DDL + GC_JOBS_DDL


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def is_legacy_database(conn: sqlite3.Connection) -> bool:
    """老库的标志：archive_files 自带 content BLOB 列。"""
    return "content" in _table_columns(conn, "archive_files")


def migrate_legacy(conn: sqlite3.Connection) -> dict:
    """把老结构（文件 BLOB 冗余在版本表和副本表里）迁到内容寻址结构。

    - 两张表里摘要相同的字节去重后落入 content_blobs；
    - archive_files / copy_files 重建为只含引用的表，主键/ID 保持不变；
    - 迁移后老版本清单、副本修复、格式迁移全部照常可用。
    要求在事务外调用（需要临时切换 foreign_keys 编译开关）。
    """
    created_at = _now()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(CONTENT_BLOBS_DDL)
        # 同摘要的字节必然相同；INSERT OR IGNORE 让重复摘要共用一个实体。
        conn.execute(
            """INSERT OR IGNORE INTO content_blobs(sha256,size,content,created_at)
               SELECT sha256, size, content, ? FROM archive_files
               UNION ALL
               SELECT sha256, size, content, ? FROM copy_files""",
            (created_at, created_at),
        )

        conn.execute("ALTER TABLE archive_files RENAME TO archive_files_legacy")
        conn.execute(ARCHIVE_FILES_DDL)
        conn.execute(
            """INSERT INTO archive_files(id,version_id,path,sha256,size)
               SELECT id,version_id,path,sha256,size FROM archive_files_legacy"""
        )
        conn.execute("DROP TABLE archive_files_legacy")

        conn.execute("ALTER TABLE copy_files RENAME TO copy_files_legacy")
        conn.execute(COPY_FILES_DDL)
        conn.execute(
            """INSERT INTO copy_files(copy_id,path,sha256,size)
               SELECT copy_id,path,sha256,size FROM copy_files_legacy"""
        )
        conn.execute("DROP TABLE copy_files_legacy")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # 必须在事务外切回；切回后做一次外键体检，避免留下悬空引用。
        conn.execute("PRAGMA foreign_keys=ON")

    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"历史数据迁移后存在悬空引用: {problems[:5]}")

    entity_count, entity_bytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM content_blobs"
    ).fetchone()
    ref_count, logical_bytes = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(size),0) FROM (
               SELECT size FROM archive_files UNION ALL SELECT size FROM copy_files
           )"""
    ).fetchone()
    return {
        "mode": "migrated",
        "entities": entity_count,
        "entity_bytes": entity_bytes,
        "references": ref_count,
        "logical_bytes": logical_bytes,
        "saved_bytes": logical_bytes - entity_bytes,
    }


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """建表并按需平滑迁移历史库，返回本次初始化/迁移的信息。"""
    stats = migrate_legacy(conn) if is_legacy_database(conn) else {"mode": "fresh"}
    conn.executescript(SCHEMA_SQL)
    return stats
