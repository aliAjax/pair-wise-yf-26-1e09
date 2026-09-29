import base64
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": b64(self.raw)},
            {"path": "README.txt", "content_b64": b64(b"archive readme")},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            b64(b"<html><body><p>1</p></body></html>"),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")

    # ---- 按内容摘要共用存储 ----

    def _blob_count(self):
        with self.store.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]

    def test_versions_copies_and_migrations_share_entities(self):
        initial_entities = self._blob_count()
        self.assertEqual(initial_entities, 2)

        # 新提交的版本内容相同：共用实体，只增加引用。
        again = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": b64(self.raw)},
            {"path": "notes.txt", "content_b64": b64(b"new note")},
        ])
        self.assertEqual(again["shared_blobs"], 1)
        self.assertEqual(self._blob_count(), 3)

        # 新登记的副本不复制字节，与版本清单指向同一实体。
        with self.store.connect() as conn:
            mismatch = conn.execute(
                """SELECT COUNT(*) FROM copy_files cf
                   JOIN archive_files af ON af.version_id=? AND af.path=cf.path
                   WHERE af.sha256<>cf.sha256""",
                (self.version["id"],),
            ).fetchone()[0]
        self.assertEqual(mismatch, 0)

        # 迁移产生的文件：未转换文件沿用引用，转换文件按摘要登记实体。
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            b64(b"<html>1</html>"),
        )
        with self.store.connect() as conn:
            same_readme = conn.execute(
                """SELECT t.sha256=s.sha256 FROM archive_files s JOIN archive_files t
                   ON t.version_id=? AND t.path='README.txt'
                   WHERE s.version_id=? AND s.path='README.txt'""",
                (migrated["id"], self.version["id"]),
            ).fetchone()[0]
        self.assertTrue(same_readme)

        # 物理实体数远少于逻辑引用数（2 版本 + 2 副本 + 迁移版本）。
        overview = self.store.storage_overview("owner")
        self.assertEqual(overview["entity_count"], self._blob_count())
        self.assertGreater(overview["reference_count"], overview["entity_count"])
        self.assertGreater(overview["logical_bytes"], overview["entity_bytes"])
        self.assertGreater(overview["shared_saved_bytes"], 0)

    # ---- 存储整理：预览、确认、被引用的不误删 ----

    def test_purge_only_removes_unreferenced_residuals(self):
        # 损坏修复后，损坏实体会残留为无人引用的实体。
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        self.store.verify_copy("owner", self.copy1)

        orphans = self.store.list_orphan_blobs("owner")
        self.assertEqual(orphans["orphan_count"], 1)
        self.assertGreater(orphans["orphan_bytes"], 0)
        bad_sha = orphans["items"][0]["sha256"]

        result = self.store.purge_orphan_blobs("owner")
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["orphan_count_after"], 0)
        self.assertEqual(result["job"]["deleted_count"], 1)

        with self.store.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM content_blobs WHERE sha256=?", (bad_sha,)
            ).fetchone())

        # 被引用的内容一个都没少：清单与副本核验照常读出完整字节。
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual({f["sha256"] for f in detail["files"]},
                         {hashlib.sha256(self.raw).hexdigest(),
                          hashlib.sha256(b"archive readme").hexdigest()})
        self.assertEqual(self.store.verify_copy("owner", self.copy1)["state"], "healthy")
        self.assertEqual(self.store.verify_copy("owner", self.copy2)["state"], "healthy")

        # 没有残留时清理是空操作。
        self.assertEqual(self.store.purge_orphan_blobs("owner")["deleted_count"], 0)

    def test_referenced_entities_are_never_purged(self):
        before = self._blob_count()
        result = self.store.purge_orphan_blobs("owner")
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(self._blob_count(), before)

    def test_storage_endpoints_require_privileged_role(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_orphan_blobs("auditor")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.purge_orphan_blobs("auditor")
        self.assertEqual(ctx.exception.status, 403)
        # 审计员可以只读看总览。
        self.assertIn("entity_bytes", self.store.storage_overview("auditor"))

    # ---- 整理与新提交并发：被引用的实体不能被清空 ----

    def test_concurrent_ingest_copy_and_purge_keeps_every_reference_readable(self):
        barrier = threading.Barrier(6)
        errors = []

        def worker(kind):
            try:
                barrier.wait()
                for _ in range(8):
                    if kind == "ingest":
                        self.store.ingest_version("owner", self.archive["id"], [
                            {"path": f"w_{kind}_{threading.get_ident()}_{_}.txt", "content_b64": b64(b"payload " + str(_).encode())},
                            {"path": "shared.txt", "content_b64": b64(self.raw)},
                        ])
                    elif kind == "copy":
                        loc = f"disk-{threading.get_ident()}-{_}"
                        copy_id = self.store.add_copy("owner", self.version["id"], loc)["id"]
                        self.assertEqual(self.store.verify_copy("owner", copy_id)["state"], "healthy")
                    else:
                        self.store.purge_orphan_blobs("owner")
            except Exception as exc:  # noqa: BLE001 - 测试中记录线程异常
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,))
                   for k in ("ingest", "ingest", "ingest", "copy", "copy", "purge")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # 所有引用都必须能读到非空实体，且字节长度与清单一致（没有空文件）。
        with self.store.connect() as conn:
            broken = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT af.sha256 s, af.size z FROM archive_files af
                       UNION ALL SELECT cf.sha256, cf.size FROM copy_files cf
                   ) r JOIN content_blobs b ON b.sha256=r.s
                   WHERE b.content IS NULL OR length(b.content)<>r.z OR r.z=0"""
            ).fetchone()[0]
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(broken, 0)
        self.assertEqual(fk_violations, [])
        self.assertEqual(self.store.verify_copy("owner", self.copy1)["state"], "healthy")
        self.assertEqual(self.store.verify_copy("owner", self.copy2)["state"], "healthy")

    # ---- 历史库平滑迁移 ----

    LEGACY_SCHEMA = """
    CREATE TABLE users(
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor'))
    );
    CREATE TABLE archives(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL REFERENCES users(id), retention_until TEXT NOT NULL,
        restricted INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE archive_members(
        archive_id INTEGER NOT NULL REFERENCES archives(id), user_id TEXT NOT NULL REFERENCES users(id),
        permission TEXT NOT NULL, PRIMARY KEY(archive_id,user_id)
    );
    CREATE TABLE archive_versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL REFERENCES archives(id),
        version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'verified',
        created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
        UNIQUE(archive_id,version)
    );
    CREATE TABLE archive_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL REFERENCES archive_versions(id),
        path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
        UNIQUE(version_id,path)
    );
    CREATE TABLE copies(
        id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL REFERENCES archive_versions(id),
        location TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'healthy',
        created_at TEXT NOT NULL, last_verified_at TEXT, UNIQUE(version_id,location)
    );
    CREATE TABLE copy_files(
        copy_id INTEGER NOT NULL REFERENCES copies(id), path TEXT NOT NULL,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY(copy_id,path)
    );
    CREATE TABLE migrations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_version_id INTEGER NOT NULL REFERENCES archive_versions(id),
        target_version_id INTEGER NOT NULL UNIQUE REFERENCES archive_versions(id),
        source_path TEXT NOT NULL, target_path TEXT NOT NULL, target_format TEXT NOT NULL,
        actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
    );
    CREATE TABLE audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL REFERENCES archives(id),
        actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
        detail TEXT NOT NULL, created_at TEXT NOT NULL
    );
    """

    def test_legacy_database_migrates_smoothly(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        now_iso = "2026-01-01T00:00:00+00:00"

        conn = sqlite3.connect(legacy_path)
        conn.executescript(self.LEGACY_SCHEMA)
        conn.execute("INSERT INTO users VALUES('owner','负责人','owner')")
        conn.execute(
            "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,1,?)",
            ("老档案", "owner", "2099-01-01", now_iso),
        )
        conn.execute(
            "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(1,1,'owner',?)",
            (now_iso,),
        )
        for path, data in [("a.xml", self.raw), ("b.txt", b"archive readme")]:
            conn.execute(
                "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(1,?,?,?,?)",
                (path, hashlib.sha256(data).hexdigest(), len(data), data),
            )
        for cid, loc in [(1, "disk-a"), (2, "disk-b")]:
            conn.execute(
                "INSERT INTO copies(id,version_id,location,created_at) VALUES(?,?,?,?)",
                (cid, 1, loc, now_iso),
            )
            conn.execute(
                """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                   SELECT ?,path,sha256,size,content FROM archive_files WHERE version_id=1""",
                (cid,),
            )
        conn.commit()
        conn.close()

        store = PreservationStore(legacy_path)
        stats = store.init_schema()
        self.assertEqual(stats["mode"], "migrated")
        # 两份副本 + 版本清单里重复的字节，迁移后只剩 2 个实体。
        self.assertEqual(stats["entities"], 2)
        self.assertEqual(stats["references"], 6)
        self.assertGreater(stats["saved_bytes"], 0)

        # 迁移是幂等的：再次初始化不再按老库处理。
        self.assertEqual(store.init_schema()["mode"], "fresh")

        # 老版本清单照常可查，摘要、大小不变。
        detail = store.get_version("owner", 1)
        self.assertEqual({f["path"]: f["size"] for f in detail["files"]},
                         {"a.xml": len(self.raw), "b.txt": len(b"archive readme")})

        # 副本核验/修复照常可用。
        self.assertEqual(store.verify_copy("owner", 1)["state"], "healthy")
        self.assertEqual(store.verify_copy("owner", 2)["state"], "healthy")
        store.simulate_corruption("owner", 1, "a.xml")
        repaired = store.verify_copy("owner", 1)
        self.assertTrue(repaired["repaired"])

        # 格式迁移照常可用，且迁移版本与老版本共用未转换文件的实体。
        migrated = store.migrate("owner", 1, "a.xml", "a.html", "html", b64(b"<html/>"))
        new_detail = store.get_version("owner", migrated["id"])
        self.assertTrue(any(f["path"] == "a.html" for f in new_detail["files"]))
        with store.connect() as c:
            shared = c.execute(
                """SELECT COUNT(*) FROM archive_files x JOIN archive_files y
                   ON x.path='b.txt' AND y.path='b.txt'
                   WHERE x.version_id=1 AND y.version_id=? AND x.sha256=y.sha256""",
                (migrated["id"],),
            ).fetchone()[0]
        self.assertEqual(shared, 1)

        # 残留实体可被整理，被引用的老数据不受影响。
        purged = store.purge_orphan_blobs("owner")
        self.assertGreaterEqual(purged["deleted_count"], 1)
        self.assertEqual(store.verify_copy("owner", 2)["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
