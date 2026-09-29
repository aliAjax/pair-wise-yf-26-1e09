"""按内容摘要共用存储的规则层。

新提交的版本文件、新登记的副本文件、迁移产生的文件都通过 intern_blob
共用同一份 content_blobs 实体：摘要相同就只登记引用，不重复写内容。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import sqlite3
from pathlib import PurePosixPath

from db import BusinessError, now

MAX_FILE_SIZE = 10 * 1024 * 1024


def verify_manifest(files: object) -> list[dict]:
    """校验清单并计算每份文件的摘要、大小，不接触数据库。"""
    if not isinstance(files, list) or not files:
        raise BusinessError("files 必须是非空数组", 422, "invalid_manifest")
    result, seen = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise BusinessError("文件条目必须是对象", 422, "invalid_manifest")
        raw_path = str(item.get("path", "")).strip().replace("\\", "/")
        pure = PurePosixPath(raw_path)
        if not raw_path or pure.is_absolute() or ".." in pure.parts or pure.name in {"", ".", ".."}:
            raise BusinessError(f"档案路径不安全: {raw_path}", 422, "unsafe_path")
        if raw_path in seen:
            raise BusinessError(f"档案路径重复: {raw_path}", 409, "duplicate_path")
        seen.add(raw_path)
        encoded = item.get("content_b64")
        if not isinstance(encoded, str):
            raise BusinessError(f"{raw_path} 缺少 content_b64", 422, "content_required")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError(f"{raw_path} 不是合法 Base64", 422, "invalid_base64")
        if len(content) > MAX_FILE_SIZE:
            raise BusinessError(f"{raw_path} 超过单文件大小限制", 413, "file_too_large")
        result.append(
            {"path": raw_path, "content": content, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        )
    return result


def intern_blob(conn: sqlite3.Connection, content: bytes, sha256: str | None = None, size: int | None = None) -> int:
    """按摘要取得内容实体 id；实体已存在就直接共用，不存在才写入一份。

    必须在写事务（BEGIN IMMEDIATE）内调用：先查后插在事务里是原子的，
    整理任务并发删除残留实体时也不会把刚建立的引用清空。
    """
    sha256 = sha256 or hashlib.sha256(content).hexdigest()
    size = len(content) if size is None else size
    row = conn.execute("SELECT id FROM content_blobs WHERE sha256=?", (sha256,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO content_blobs(sha256,size,content,created_at) VALUES(?,?,?,?)",
        (sha256, size, content, now()),
    )
    return cur.lastrowid


def intern_file(
    conn: sqlite3.Connection,
    table: str,
    key_cols: tuple[str, ...],
    key_vals: tuple,
    path: str,
    sha256: str,
    size: int,
    content: bytes,
) -> None:
    """登记一条文件引用（archive_files/copy_files），实体共用。"""
    blob_id = intern_blob(conn, content, sha256, size)
    placeholders = ",".join("?" * len(key_cols))
    conn.execute(
        f"""INSERT INTO {table}({','.join(key_cols)},path,sha256,size,blob_id)
            VALUES({placeholders},?,?,?,?)""",
        (*key_vals, path, sha256, size, blob_id),
    )


def orphan_blobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """找出没有任何版本清单或副本引用的残留内容实体，按大小降序。"""
    return conn.execute(
        """
        SELECT b.id,b.sha256,b.size,b.created_at
        FROM content_blobs b
        WHERE NOT EXISTS(SELECT 1 FROM archive_files f WHERE f.blob_id=b.id)
          AND NOT EXISTS(SELECT 1 FROM copy_files c WHERE c.blob_id=b.id)
        ORDER BY b.size DESC,b.id
        """
    ).fetchall()


def storage_overview(conn: sqlite3.Connection) -> dict:
    """存储盘点：实体数、占用空间，以及去重前后的对比。"""
    total = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM content_blobs").fetchone()
    referenced = conn.execute(
        """SELECT COUNT(DISTINCT b.id) AS n, COALESCE(SUM(b.size),0) AS bytes
           FROM content_blobs b
           WHERE EXISTS(SELECT 1 FROM archive_files f WHERE f.blob_id=b.id)
              OR EXISTS(SELECT 1 FROM copy_files c WHERE c.blob_id=b.id)"""
    ).fetchone()
    version_refs = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM archive_files").fetchone()
    copy_refs = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM copy_files").fetchone()
    orphans = orphan_blobs(conn)
    return {
        "blob_count": total["n"],
        "physical_bytes": total["bytes"],
        "referenced_blob_count": referenced["n"],
        "referenced_bytes": referenced["bytes"],
        "orphan_blob_count": len(orphans),
        "orphan_bytes": sum(r["size"] for r in orphans),
        "logical_version_bytes": version_refs["bytes"],
        "logical_copy_bytes": copy_refs["bytes"],
        "version_file_count": version_refs["n"],
        "copy_file_count": copy_refs["n"],
        "dedup_saved_bytes": version_refs["bytes"] + copy_refs["bytes"] - total["bytes"],
    }
