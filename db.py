"""数据表与连接：SQLite 连接、核心表结构、通用错误与审计写入。

内容去重规则见 content.py，整理任务表见 cleanup.py，历史库结构升级见 migration.py。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "preservation.db"
SCHEMA_VERSION = 2


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# V2 结构：文件内容只存一份实体（content_blobs），版本清单与副本只登记 blob_id 引用。
CORE_DDL = """
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
CREATE TABLE IF NOT EXISTS content_blobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL CHECK(size >= 0),
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
    UNIQUE(version_id,path)
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
CREATE TABLE IF NOT EXISTS copy_files(
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
    PRIMARY KEY(copy_id,path)
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
    archive_id INTEGER REFERENCES archives(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def exec_ddl(conn: sqlite3.Connection, script: str) -> None:
    """逐条执行 DDL。

    不用 executescript：它在执行前会隐式 COMMIT，会破坏历史迁移的
    单事务原子性。所有 DDL 语句内均无分号字面量，按 ';' 切分即可。
    """
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def create_core_tables(conn: sqlite3.Connection) -> None:
    exec_ddl(conn, CORE_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
        (SCHEMA_VERSION, now()),
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def write_audit(
    conn: sqlite3.Connection,
    archive_id: int | None,
    actor_id: str,
    action: str,
    detail: dict,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (archive_id, actor_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
    )
