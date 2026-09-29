import base64
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from db import connect
from db import BusinessError
from store import PreservationStore


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


class SharedStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = PreservationStore(self.db)
        self.store.seed()
        self.a = self.store.create_archive("owner", "档案甲", (date.today() + timedelta(days=365)).isoformat())["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_versions_copies_and_migrations_share_blobs(self):
        shared = b"<record><id>1</id></record>"
        v1 = self.store.ingest_version("owner", self.a, [
            {"path": "one.xml", "content_b64": b64(shared)},
            {"path": "two.txt", "content_b64": b64(b"distinct-two")},
        ])
        # 新版本重复提交同样内容 + 登记副本
        v2 = self.store.ingest_version("owner", self.a, [
            {"path": "copy-of-one.xml", "content_b64": b64(shared)},
        ])
        self.assertEqual(v2["shared_blobs"], 1)
        self.store.add_copy("owner", v1["id"], "disk-a")
        self.store.add_copy("owner", v2["id"], "disk-b")
        # 迁移：未变文件沿用引用，转换后内容若与既有实体同摘要也共用
        migrated = self.store.migrate("owner", v2["id"], "copy-of-one.xml", "one.html", "html",
                                      b64(b"<html>x</html>"))
        # 再迁移一次，目标内容与上次相同
        self.store.migrate("owner", migrated["id"], "one.html", "one2.html", "html", b64(b"<html>x</html>"))

        with connect(self.db) as conn:
            blobs = conn.execute("SELECT COUNT(*) AS n FROM content_blobs").fetchone()["n"]
            refs = conn.execute(
                "SELECT (SELECT COUNT(*) FROM archive_files)+(SELECT COUNT(*) FROM copy_files) AS n"
            ).fetchone()["n"]
            blob_ids = {r[0] for r in conn.execute("SELECT blob_id FROM archive_files UNION SELECT blob_id FROM copy_files")}
        # 4 种不同内容（shared、two、html、……），引用数远多于实体数，且所有引用都指向有效实体
        self.assertLess(blobs, refs)
        self.assertEqual(len(blob_ids), blobs)

        overview = self.store.storage_status("owner")
        self.assertEqual(overview["orphan_blob_count"], 0)
        self.assertGreaterEqual(overview["dedup_saved_bytes"], len(shared))  # 至少共用了 shared
        # 所有引用读到的内容都与摘要一致
        detail = self.store.get_version("owner", v2["id"])
        self.assertEqual(detail["files"][0]["sha256"], hashlib.sha256(shared).hexdigest())

    def test_cleanup_deletes_only_unreferenced_and_skips_newly_referenced(self):
        payload = b"unique-orphan-content"
        v = self.store.ingest_version("owner", self.a, [{"path": "x.bin", "content_b64": b64(payload)}])
        self.store.add_copy("owner", v["id"], "disk-a")
        # 直接制造残留：版本与副本都不再引用，但实体还在（历史残留场景）
        with connect(self.db) as conn:
            conn.execute("DELETE FROM copy_files")
            conn.execute("DELETE FROM archive_files")
        overview = self.store.storage_status("owner")
        self.assertEqual(overview["orphan_blob_count"], 1)
        self.assertEqual(overview["orphan_bytes"], len(payload))

        plan = self.store.create_cleanup_plan("archivist")
        self.assertEqual(plan["state"], "planned")
        self.assertEqual(plan["planned_blob_count"], 1)
        # 审计员只读，不能生成/确认计划
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_cleanup_plan("auditor")
        self.assertEqual(ctx.exception.status, 403)

        # 计划确认前，新提交相同内容重新引用了该实体
        v2 = self.store.ingest_version("owner", self.a, [{"path": "y.bin", "content_b64": b64(payload)}])
        result = self.store.confirm_cleanup_task("owner", plan["id"])
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["deleted_blob_count"], 0)
        self.assertTrue(all(i["state"] == "skipped_referenced" for i in result["items"]))
        # 新版本清单必须读到完整内容，不能是空文件
        with connect(self.db) as conn:
            row = conn.execute(
                """SELECT b.content FROM archive_files f JOIN content_blobs b ON b.id=f.blob_id
                   WHERE f.version_id=? AND path='y.bin'""", (v2["id"],)
            ).fetchone()
        self.assertEqual(row["content"], payload)
        # 不能重复确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_cleanup_task("owner", plan["id"])
        self.assertEqual(ctx.exception.code, "task_closed")

    def test_cleanup_remains_safe_under_concurrent_ingest(self):
        """整理确认与新提交并发：任何时序下被引用的实体都不能被清空。"""
        payload = b"concurrent-orphan-payload"
        v = self.store.ingest_version("owner", self.a, [{"path": "x.bin", "content_b64": b64(payload)}])
        with connect(self.db) as conn:
            conn.execute("DELETE FROM copy_files")
            conn.execute("DELETE FROM archive_files")
        plan = self.store.create_cleanup_plan("owner")

        errors = []

        def confirm():
            try:
                barrier.wait()
                self.store.confirm_cleanup_task("owner", plan["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def ingest():
            try:
                barrier.wait()
                self.store.ingest_version("owner", self.a, [{"path": "y.bin", "content_b64": b64(payload)}])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=confirm)
        t2 = threading.Thread(target=ingest)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])

        # 最终不变量：所有引用都能 join 到实体，且读出的就是原内容
        with connect(self.db) as conn:
            rows = conn.execute(
                """SELECT f.path,b.content FROM archive_files f JOIN content_blobs b ON b.id=f.blob_id
                   WHERE f.path='y.bin'"""
            ).fetchall()
            dangling = conn.execute(
                """SELECT COUNT(*) AS n FROM archive_files f LEFT JOIN content_blobs b ON b.id=f.blob_id
                   WHERE b.id IS NULL"""
            ).fetchone()["n"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], payload)
        self.assertEqual(dangling, 0)

    def test_orphan_from_corruption_is_cleanable_after_repair(self):
        v = self.store.ingest_version("owner", self.a, [{"path": "f.bin", "content_b64": b64(b"media-bytes")}])
        c1 = self.store.add_copy("owner", v["id"], "d1")["id"]
        c2 = self.store.add_copy("owner", v["id"], "d2")["id"]
        self.store.simulate_corruption("owner", c1, "f.bin")
        self.assertEqual(self.store.verify_copy("owner", c1)["state"], "healthy")
        # 修复后损坏实体成为残留
        overview = self.store.storage_status("owner")
        self.assertEqual(overview["orphan_blob_count"], 1)
        plan = self.store.create_cleanup_plan("owner")
        done = self.store.confirm_cleanup_task("owner", plan["id"])
        self.assertEqual(done["deleted_blob_count"], 1)
        # 被引用的健康实体仍在，副本核验依旧健康
        self.assertEqual(self.store.verify_copy("owner", c1)["state"], "healthy")
        self.assertEqual(self.store.verify_copy("owner", c2)["state"], "healthy")
        self.assertEqual(self.store.storage_status("owner")["orphan_blob_count"], 0)


# V1 老结构：内容内联在 archive_files/copy_files 的 content BLOB 里。
V1_DDL = """
CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor')));
CREATE TABLE archives(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL REFERENCES users(id), retention_until TEXT NOT NULL, restricted INTEGER NOT NULL DEFAULT 1 CHECK(restricted IN (0,1)), created_at TEXT NOT NULL);
CREATE TABLE archive_members(archive_id INTEGER NOT NULL REFERENCES archives(id), user_id TEXT NOT NULL REFERENCES users(id), permission TEXT NOT NULL CHECK(permission IN ('read','write')), PRIMARY KEY(archive_id,user_id));
CREATE TABLE archive_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL REFERENCES archives(id), version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'verified' CHECK(state IN ('verified','degraded')), created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(archive_id,version));
CREATE TABLE archive_files(id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL REFERENCES archive_versions(id), path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL, UNIQUE(version_id,path));
CREATE TABLE copies(id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL REFERENCES archive_versions(id), location TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','degraded')), created_at TEXT NOT NULL, last_verified_at TEXT, UNIQUE(version_id,location));
CREATE TABLE copy_files(copy_id INTEGER NOT NULL REFERENCES copies(id), path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL, PRIMARY KEY(copy_id,path));
CREATE TABLE migrations(id INTEGER PRIMARY KEY AUTOINCREMENT, source_version_id INTEGER NOT NULL REFERENCES archive_versions(id), target_version_id INTEGER NOT NULL UNIQUE REFERENCES archive_versions(id), source_path TEXT NOT NULL, target_path TEXT NOT NULL, target_format TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL REFERENCES archives(id), actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "legacy.db"
        self.payload = b"<legacy><v>1</v></legacy>"
        self.readme = b"legacy readme"
        conn = sqlite3.connect(self.db)
        conn.executescript(V1_DDL)
        conn.execute("INSERT INTO users VALUES('owner','负责人','owner')")
        conn.execute("INSERT INTO users VALUES('archivist','管理员','archivist')")
        conn.execute("INSERT INTO users VALUES('auditor','审计','auditor')")
        retention = (date.today() + timedelta(days=365)).isoformat()
        conn.execute("INSERT INTO archives(id,name,owner_id,retention_until,restricted,created_at) VALUES(1,'老档案','owner',?,1,'2020-01-01T00:00:00+00:00')", (retention,))
        conn.execute("INSERT INTO archive_members VALUES(1,'owner','write')")
        conn.execute("INSERT INTO archive_versions(id,archive_id,version,created_by,created_at) VALUES(1,1,1,'owner','2020-01-01T00:00:00+00:00')")
        for path, data in [("old/one.xml", self.payload), ("README.txt", self.readme)]:
            conn.execute("INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(1,?,?,?,?)",
                         (path, hashlib.sha256(data).hexdigest(), len(data), data))
        conn.execute("INSERT INTO copies(id,version_id,location,state,created_at,last_verified_at) VALUES(1,1,'old-disk','healthy','2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO copies(id,version_id,location,state,created_at,last_verified_at) VALUES(2,1,'old-disk-2','healthy','2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00')")
        for cid in (1, 2):
            for path, data in [("old/one.xml", self.payload), ("README.txt", self.readme)]:
                conn.execute("INSERT INTO copy_files(copy_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                             (cid, path, hashlib.sha256(data).hexdigest(), len(data), data))
        conn.execute("INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(1,'owner','legacy.event','{}','2020-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_data_migrates_and_old_flows_keep_working(self):
        store = PreservationStore(self.db)
        stats = store.init_schema()
        self.assertIsNotNone(stats)
        self.assertEqual(stats["content_blobs"], 2)  # 两份不同内容只落成两个实体
        self.assertEqual(stats["archive_files"], 2)
        self.assertEqual(stats["copy_files"], 4)
        # 幂等：再次初始化不再迁移
        self.assertIsNone(store.init_schema())

        # 老版本清单照常，且老副本与版本共用同一实体
        detail = store.get_version("owner", 1)
        self.assertEqual({f["path"] for f in detail["files"]}, {"old/one.xml", "README.txt"})
        with connect(self.db) as conn:
            shared = conn.execute(
                """SELECT COUNT(DISTINCT b.id) AS n FROM content_blobs b
                   WHERE EXISTS(SELECT 1 FROM archive_files f WHERE f.blob_id=b.id)
                     AND EXISTS(SELECT 1 FROM copy_files c WHERE c.blob_id=b.id)"""
            ).fetchone()["n"]
            old_audit = conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='legacy.event'").fetchone()["n"]
        self.assertEqual(shared, 2)
        self.assertEqual(old_audit, 1)

        # 老副本核验、损坏修复照常
        self.assertEqual(store.verify_copy("owner", 1)["state"], "healthy")
        store.simulate_corruption("owner", 1, "old/one.xml")
        repaired = store.verify_copy("owner", 1)
        self.assertTrue(repaired["repaired"])
        self.assertEqual(repaired["state"], "healthy")

        # 基于老版本的格式迁移照常，且未转换文件零拷贝沿用
        migrated = store.migrate("owner", 1, "old/one.xml", "old/one.html", "html",
                                 b64(b"<html>legacy</html>"))
        v2 = store.get_version("owner", migrated["id"])
        self.assertEqual({f["path"] for f in v2["files"]}, {"old/one.html", "README.txt"})

        # 历史数据本身已完成共用：2 个老内容实体 + 1 个迁移新内容实体，
        # 修复后损坏副本改指的损坏实体成为 1 个残留。
        overview = store.storage_status("owner")
        self.assertEqual(overview["blob_count"], 4)
        self.assertEqual(overview["orphan_blob_count"], 1)
        # 残留的正是损坏实体；清掉后全部实体都有引用
        plan = store.create_cleanup_plan("owner")
        done = store.confirm_cleanup_task("owner", plan["id"])
        self.assertEqual(done["deleted_blob_count"], 1)
        self.assertEqual(store.storage_status("owner")["orphan_blob_count"], 0)


if __name__ == "__main__":
    unittest.main()
