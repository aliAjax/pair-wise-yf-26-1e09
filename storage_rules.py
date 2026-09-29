"""按内容摘要共用存储的规则层。

规则：任何字节都先按 SHA-256 摘要找实体（content_blobs），找到就共用，
找不到才登记新实体；版本文件和副本文件都只登记对实体的引用，不再持有字节。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_blob(conn: sqlite3.Connection, sha256: str, size: int, content: bytes) -> bool:
    """按摘要登记实体；摘要相同则共用已有实体，不重复写字节。

    返回是否新建了实体（False 表示命中共用）。
    """
    cur = conn.execute(
        """INSERT INTO content_blobs(sha256,size,content,created_at) VALUES(?,?,?,?)
           ON CONFLICT(sha256) DO NOTHING""",
        (sha256, size, content, _now()),
    )
    return cur.rowcount > 0


def register_version_file(
    conn: sqlite3.Connection, version_id: int, path: str, sha256: str, size: int
) -> None:
    """版本清单只登记对内容实体的引用。"""
    conn.execute(
        "INSERT INTO archive_files(version_id,path,sha256,size) VALUES(?,?,?,?)",
        (version_id, path, sha256, size),
    )


def clone_version_files(conn: sqlite3.Connection, source_version_id: int, target_version_id: int,
                        exclude_path: str | None = None) -> int:
    """把源版本的引用登记到目标版本（迁移保留未转换文件），只复制引用不复制字节。"""
    if exclude_path is None:
        sql = ("INSERT INTO archive_files(version_id,path,sha256,size) "
               "SELECT ?,path,sha256,size FROM archive_files WHERE version_id=?")
        params: tuple = (target_version_id, source_version_id)
    else:
        sql = ("INSERT INTO archive_files(version_id,path,sha256,size) "
               "SELECT ?,path,sha256,size FROM archive_files WHERE version_id=? AND path<>?")
        params = (target_version_id, source_version_id, exclude_path)
    return conn.execute(sql, params).rowcount


def register_copy_from_version(conn: sqlite3.Connection, copy_id: int, version_id: int) -> int:
    """新登记副本时按清单登记引用，与版本共用同一份实体，返回登记的引用数。"""
    return conn.execute(
        """INSERT INTO copy_files(copy_id,path,sha256,size)
           SELECT ?,path,sha256,size FROM archive_files WHERE version_id=?""",
        (copy_id, version_id),
    ).rowcount


def repoint_copy_from_donor(conn: sqlite3.Connection, copy_id: int, donor_copy_id: int) -> None:
    """修复副本：清空被损坏的引用，改指健康副本所引用的同一批实体。"""
    conn.execute("DELETE FROM copy_files WHERE copy_id=?", (copy_id,))
    conn.execute(
        """INSERT INTO copy_files(copy_id,path,sha256,size)
           SELECT ?,path,sha256,size FROM copy_files WHERE copy_id=?""",
        (copy_id, donor_copy_id),
    )


def materialize_corruption(
    conn: sqlite3.Connection, copy_id: int, path: str
) -> sqlite3.Row | None:
    """模拟介质损坏：损坏的引用不能污染共享实体，另存一个独立的坏实体并改指它。

    返回更新后的引用行（sha256,size），文件不存在返回 None。
    """
    row = conn.execute(
        """SELECT b.content FROM copy_files cf
           JOIN content_blobs b ON b.sha256=cf.sha256
           WHERE cf.copy_id=? AND cf.path=?""",
        (copy_id, path),
    ).fetchone()
    if row is None:
        return None
    original = row["content"]
    damaged = bytes([original[0] ^ 0xFF]) + original[1:] if original else b"corrupt"
    bad_digest = hashlib.sha256(damaged).hexdigest()
    put_blob(conn, bad_digest, len(damaged), damaged)
    conn.execute(
        "UPDATE copy_files SET sha256=?,size=? WHERE copy_id=? AND path=?",
        (bad_digest, len(damaged), copy_id, path),
    )
    return conn.execute(
        "SELECT sha256,size FROM copy_files WHERE copy_id=? AND path=?", (copy_id, path)
    ).fetchone()
