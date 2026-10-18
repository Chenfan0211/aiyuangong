# -*- coding: utf-8 -*-
"""加密备份、异地存储适配与恢复演练测试。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup_manager import (
    BackupIntegrityError,
    BackupManager,
    BackupProductionRestoreError,
    LocalBackupStorage,
    S3BackupStorage,
    derive_encryption_key,
    load_or_create_encryption_key,
)
from db import Database
from task_store import TaskStore

try:
    import pgserver
    import psycopg

    HAS_PG = True
except ImportError:  # pragma: no cover - 由环境决定
    HAS_PG = False


TENANT = "LPL-001"
STORE = "WH-LION-001"
TASK_ID = "T-BACKUP-001"


def seed_database(target) -> None:
    tasks = TaskStore(target, tenant_id=TENANT)
    tasks.save_plan(
        {
            "date": "2026-10-19",
            "tasks": [
                {
                    "task_id": TASK_ID,
                    "category": "reputation",
                    "title": "处理差评并提交证据",
                    "assignee": "店长",
                    "priority": "high",
                    "due_at": "2026-10-19T18:00:00",
                    "target": {"review_id": "R-1"},
                    "evidence_required": ["平台回复截图"],
                    "reason": "测试备份",
                    "requires_approval": False,
                }
            ],
        },
        STORE,
    )


def write_source_files(root: Path) -> tuple[Path, Path]:
    evidence = root / "source-evidence"
    evidence.mkdir(parents=True)
    (evidence / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nbackup-evidence")
    (evidence / "nested").mkdir()
    (evidence / "nested" / "note.txt").write_text("保留证据说明", encoding="utf-8")
    config = root / "store_config.json"
    config.write_text(
        json.dumps({"tenant_id": TENANT, "store_id": STORE, "name": "武汉狮子座店"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return evidence, config


class FakeS3Client:
    """只实现 backup_manager 需要的最小 S3 契约。"""

    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, Metadata=None, **kwargs):
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = {
            "Body": data,
            "Metadata": {str(k): str(v) for k, v in (Metadata or {}).items()},
            "LastModified": datetime(2026, 10, 19, tzinfo=timezone.utc),
        }
        self.calls.append(("put_object", Key))
        return {"ETag": hashlib.md5(data).hexdigest()}

    def get_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        self.calls.append(("get_object", Key))
        return {
            "Body": io.BytesIO(item["Body"]),
            "Metadata": dict(item["Metadata"]),
            "ContentLength": len(item["Body"]),
            "LastModified": item["LastModified"],
        }

    def head_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        self.calls.append(("head_object", Key))
        return {
            "Metadata": dict(item["Metadata"]),
            "ContentLength": len(item["Body"]),
            "LastModified": item["LastModified"],
        }

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, MaxKeys=1000):
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + MaxKeys]
        response = {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[(Bucket, key)]["Body"]),
                    "LastModified": self.objects[(Bucket, key)]["LastModified"],
                }
                for key in page
            ],
            "IsTruncated": start + len(page) < len(keys),
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + len(page))
        self.calls.append(("list_objects_v2", Prefix))
        return response

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        self.calls.append(("delete_object", Key))
        return {}


class BackupManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ops.db"
        seed_database(self.db)
        self.evidence, self.config = write_source_files(self.root)
        self.key = derive_encryption_key("unit-test-backup-key")
        self.storage = LocalBackupStorage(self.root / "storage")
        self.manager = BackupManager(self.storage, encryption_key=self.key)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **overrides):
        params = {
            "tenant_id": TENANT,
            "store_id": STORE,
            "evidence_dir": self.evidence,
            "config_path": self.config,
            "now": datetime(2026, 10, 19, 8, 30, 0),
        }
        params.update(overrides)
        return self.manager.create_backup(self.db, **params)


class LocalStorageTests(BackupManagerTestCase):
    def test_local_roundtrip_list_and_integrity(self):
        key = "daily/2026/10/19/example.aiygbk"
        info = self.storage.put(
            key,
            b"encrypted-bytes",
            metadata={"category": "daily", "created_at": "2026-10-19T08:30:00"},
        )
        self.assertEqual(info["sha256"], hashlib.sha256(b"encrypted-bytes").hexdigest())
        self.assertEqual(self.storage.get(key), b"encrypted-bytes")
        self.assertEqual([item["key"] for item in self.storage.list("daily/2026/10")], [key])

        path = self.root / "storage" / key
        path.write_bytes(b"tampered")
        with self.assertRaises(BackupIntegrityError):
            self.storage.get(key)
        self.assertTrue(self.storage.delete(key))
        self.assertFalse(self.storage.exists(key))

    def test_local_storage_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            self.storage.put("../escape.aiygbk", b"x")
        with self.assertRaises(ValueError):
            self.storage.put("daily/../../escape.aiygbk", b"x")


class EncryptionAndBackupTests(BackupManagerTestCase):
    def test_encrypted_backup_has_daily_and_monthly_objects_and_sha256(self):
        result = self.create()
        daily = result["daily"]
        monthly = result["monthly"]
        self.assertTrue(result["ok"])
        self.assertRegex(daily["key"], r"^daily/2026/10/19/[0-9T-]+-[0-9a-f]+\.aiygbk$")
        self.assertRegex(monthly["key"], r"^monthly/2026/10/[0-9T-]+-[0-9a-f]+\.aiygbk$")
        self.assertEqual(daily["sha256"], hashlib.sha256(self.storage.get(daily["key"])).hexdigest())

        encrypted = self.storage.get(daily["key"])
        self.assertNotIn(b"store_config", encrypted)
        self.assertNotIn("备份证据".encode("utf-8"), encrypted)

        verified = self.manager.verify(daily["key"])
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(verified["manifest"]["tenant_id"], TENANT)
        self.assertEqual(verified["manifest"]["store_id"], STORE)
        self.assertGreaterEqual(verified["manifest"]["database"]["table_count"], 1)
        self.assertGreaterEqual(verified["manifest"]["evidence_count"], 2)
        self.assertTrue(verified["manifest"]["config"]["included"])

    def test_wrong_key_and_ciphertext_tampering_are_rejected(self):
        key = self.create()["daily"]["key"]
        wrong = BackupManager(self.storage, encryption_key=derive_encryption_key("wrong-key"))
        with self.assertRaises(BackupIntegrityError):
            wrong.verify(key)

        path = self.root / "storage" / key
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0x01
        path.write_bytes(bytes(raw))
        with self.assertRaises(BackupIntegrityError):
            self.manager.verify(key)

    def test_local_key_is_created_once_without_third_party_secret(self):
        key_file = self.root / "backup.key"
        first = load_or_create_encryption_key(key_file)
        second = load_or_create_encryption_key(key_file)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        manager = BackupManager(LocalBackupStorage(self.root / "keyed-storage"), key_file=key_file)
        result = manager.create_backup(
            self.db,
            tenant_id=TENANT,
            store_id=STORE,
            evidence_dir=self.evidence,
            config_path=self.config,
            now=datetime(2026, 10, 19, 9, 0, 0),
        )
        self.assertTrue(manager.verify(result["daily"]["key"])["ok"])


class RestoreTests(BackupManagerTestCase):
    def test_restore_to_fresh_sqlite_restores_database_evidence_and_config(self):
        key = self.create()["daily"]["key"]
        target_db = self.root / "restore" / "ops.db"
        target_evidence = self.root / "restore" / "evidence"
        target_config = self.root / "restore" / "store_config.json"

        result = self.manager.restore(
            key,
            target_db=target_db,
            evidence_dir=target_evidence,
            config_path=target_config,
            tenant_id=TENANT,
            store_id=STORE,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(TaskStore(target_db, tenant_id=TENANT).get_task(TASK_ID)["title"], "处理差评并提交证据")
        self.assertEqual((target_evidence / "photo.png").read_bytes(), (self.evidence / "photo.png").read_bytes())
        self.assertEqual((target_evidence / "nested" / "note.txt").read_text(encoding="utf-8"), "保留证据说明")
        self.assertEqual(json.loads(target_config.read_text(encoding="utf-8"))["store_id"], STORE)
        self.assertTrue(result["database"]["verified"])
        self.assertEqual(result["evidence"]["restored_files"], 2)

    def test_restore_refuses_source_or_non_empty_target(self):
        key = self.create()["daily"]["key"]
        with self.assertRaises(BackupProductionRestoreError):
            self.manager.restore(key, target_db=self.db, tenant_id=TENANT, store_id=STORE)

        other = self.root / "other.db"
        seed_database(other)
        with self.assertRaises(BackupProductionRestoreError):
            self.manager.restore(key, target_db=other, tenant_id=TENANT, store_id=STORE)

    def test_restore_requires_expected_sha256_when_storage_metadata_is_gone(self):
        key = self.create()["daily"]["key"]
        (self.root / "storage" / (key + ".meta.json")).unlink()
        with self.assertRaises(BackupIntegrityError):
            self.manager.restore(key, target_db=self.root / "missing-hash.db")
        with self.assertRaises(BackupIntegrityError):
            self.manager.restore(key, target_db=self.root / "bad-hash.db", expected_sha256="0" * 64)

    def test_drill_restores_into_fresh_database_and_verifies_content(self):
        report = self.manager.drill(
            self.db,
            evidence_dir=self.evidence,
            config_path=self.config,
            tenant_id=TENANT,
            store_id=STORE,
            now=datetime(2026, 10, 19, 10, 0, 0),
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["database_verified"])
        self.assertTrue(report["evidence_verified"])
        self.assertTrue(report["config_verified"])
        self.assertEqual(report["restored_files"], 2)


class RetentionTests(BackupManagerTestCase):
    def test_retention_keeps_thirty_daily_and_twelve_monthly(self):
        for index in range(35):
            moment = datetime(2026, 9, 1) + timedelta(days=index)
            self.storage.put(
                f"daily/{moment:%Y/%m/%d}/backup-{index:02d}.aiygbk",
                f"d{index}".encode(),
                metadata={"category": "daily", "created_at": moment.isoformat()},
            )
        for index in range(15):
            moment = datetime(2025, 8, 1) + timedelta(days=31 * index)
            self.storage.put(
                f"monthly/{moment:%Y/%m}/backup-{index:02d}.aiygbk",
                f"m{index}".encode(),
                metadata={"category": "monthly", "created_at": moment.isoformat()},
            )

        deleted_daily = self.manager.prune("daily", keep=30)
        deleted_monthly = self.manager.prune("monthly", keep=12)
        self.assertEqual(deleted_daily, 5)
        self.assertEqual(deleted_monthly, 3)
        self.assertEqual(len(self.storage.list("daily/")), 30)
        self.assertEqual(len(self.storage.list("monthly/")), 12)


class S3AdapterTests(BackupManagerTestCase):
    def test_s3_contract_with_fake_client_supports_backup_and_restore(self):
        fake = FakeS3Client()
        storage = S3BackupStorage("backup-bucket", prefix="pilot/wh-lion-001", client=fake)
        manager = BackupManager(storage, encryption_key=self.key)
        result = manager.create_backup(
            self.db,
            tenant_id=TENANT,
            store_id=STORE,
            evidence_dir=self.evidence,
            config_path=self.config,
            now=datetime(2026, 10, 19, 11, 0, 0),
        )
        daily_key = result["daily"]["key"]
        self.assertTrue(daily_key.startswith("pilot/wh-lion-001/daily/2026/10/19/"))
        self.assertIn(("put_object", daily_key), fake.calls)
        self.assertEqual(manager.verify(daily_key)["sha256"], result["daily"]["sha256"])

        target = self.root / "s3-restore.db"
        restored = manager.restore(daily_key, target_db=target, tenant_id=TENANT, store_id=STORE)
        self.assertTrue(restored["ok"])
        self.assertEqual(TaskStore(target, tenant_id=TENANT).get_task(TASK_ID)["task_id"], TASK_ID)

    def test_s3_storage_metadata_roundtrip_and_prefix_delete(self):
        fake = FakeS3Client()
        storage = S3BackupStorage("bucket", prefix="ops", client=fake)
        key = "daily/2026/10/19/a.aiygbk"
        info = storage.put(key, b"payload", metadata={"category": "daily", "created_at": "2026-10-19T11:00:00"})
        self.assertEqual(info["key"], "ops/" + key)
        listed = storage.list("daily/")
        self.assertEqual(listed[0]["key"], "ops/" + key)
        self.assertEqual(storage.get("ops/" + key), b"payload")
        self.assertTrue(storage.delete("ops/" + key))
        self.assertFalse(storage.exists("ops/" + key))


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 演练")
class PostgresBackupManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_backup_manager_pg_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        seed_database(self.dsn)

    def test_postgres_encrypted_backup_drill_uses_fresh_database(self):
        root = Path(tempfile.mkdtemp(prefix="aiyg_backup_manager_case_"))
        self.addCleanup(shutil.rmtree, root, True)
        evidence, config = write_source_files(root)
        manager = BackupManager(
            LocalBackupStorage(root / "storage"),
            encryption_key=derive_encryption_key("postgres-drill-key"),
        )
        report = manager.drill(
            self.dsn,
            evidence_dir=evidence,
            config_path=config,
            tenant_id=TENANT,
            store_id=STORE,
            now=datetime(2026, 10, 19, 12, 0, 0),
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["database_verified"])
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from action_tasks where task_id=%s", (TASK_ID,))
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute("select count(*) from pg_database where datname like 'aiyg_bm_drill_%'")
            self.assertEqual(int(cur.fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
