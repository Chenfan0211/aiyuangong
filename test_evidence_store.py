# -*- coding: utf-8 -*-
"""图片证据存储模块测试。"""

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from db import Database
from evidence_store import (
    MAX_EVIDENCE_BYTES,
    EvidenceMimeError,
    EvidenceNotFoundError,
    EvidencePathError,
    EvidenceSizeError,
    EvidenceStore,
)

try:
    import pgserver
    import psycopg

    HAS_PG = True
except ImportError:  # pragma: no cover - 由环境决定
    HAS_PG = False


TENANT = "LPL-001"
OTHER_TENANT = "LPL-002"
STORE = "WH-LION-001"
TASK = "T-20261018-001"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-evidence"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-evidence"
WEBP = b"RIFF" + (len(b"webp-evidence") + 4).to_bytes(4, "little") + b"WEBP" + b"webp-evidence"


class SqliteEvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "ops.db"
        self.evidence_root = self.root / "evidence"
        self.store = EvidenceStore(self.db_path, root_dir=self.evidence_root, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def save_image(self, data=PNG, name="门店照片.png", **overrides):
        params = dict(
            task_id=TASK,
            original_name=name,
            data=data,
            uploaded_by="employee01",
            store_id=STORE,
            tenant_id=TENANT,
        )
        params.update(overrides)
        return self.store.save(**params)

    def test_save_and_read_png_evidence(self):
        record = self.save_image()

        self.assertEqual(record["tenant_id"], TENANT)
        self.assertEqual(record["store_id"], STORE)
        self.assertEqual(record["task_id"], TASK)
        self.assertEqual(record["original_name"], "门店照片.png")
        self.assertEqual(record["mime_type"], "image/png")
        self.assertEqual(record["size_bytes"], len(PNG))
        self.assertEqual(record["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(record["uploaded_by"], "employee01")
        self.assertEqual(Path(record["stored_path"]).name, record["stored_path"])
        self.assertTrue(record["stored_path"].endswith(".png"))
        self.assertTrue((self.evidence_root / record["stored_path"]).is_file())

        self.assertEqual(
            self.store.read(
                record["evidence_id"],
                tenant_id=TENANT,
                store_id=STORE,
                task_id=TASK,
            ),
            PNG,
        )
        listed = self.store.list_evidence(tenant_id=TENANT, store_id=STORE, task_id=TASK)
        self.assertEqual([item["evidence_id"] for item in listed], [record["evidence_id"]])

    def test_supports_jpeg_and_webp_magic_bytes(self):
        for data, expected_mime, suffix in (
            (JPEG, "image/jpeg", ".jpg"),
            (WEBP, "image/webp", ".webp"),
        ):
            with self.subTest(mime=expected_mime):
                record = self.save_image(data=data, name=f"evidence{suffix}")
                self.assertEqual(record["mime_type"], expected_mime)
                self.assertTrue(record["stored_path"].endswith(suffix))
                self.assertEqual(
                    self.store.read(
                        record["evidence_id"],
                        tenant_id=TENANT,
                        store_id=STORE,
                        task_id=TASK,
                    ),
                    data,
                )

    def test_rejects_non_image_content(self):
        with self.assertRaises(EvidenceMimeError):
            self.save_image(data=b"not-an-image", name="fake.png")
        self.assertEqual([p for p in self.evidence_root.rglob("*") if p.is_file()], [])

    def test_rejects_declared_mime_mismatch(self):
        with self.assertRaises(EvidenceMimeError):
            self.save_image(data=PNG, name="photo.png", mime_type="image/jpeg")
        self.assertEqual([p for p in self.evidence_root.rglob("*") if p.is_file()], [])

    def test_rejects_file_larger_than_limit(self):
        too_large = PNG + b"x" * (MAX_EVIDENCE_BYTES + 1 - len(PNG))
        with self.assertRaises(EvidenceSizeError):
            self.save_image(data=too_large, name="too-large.png")
        self.assertEqual([p for p in self.evidence_root.rglob("*") if p.is_file()], [])

    def test_rejects_path_traversal_original_name(self):
        for name in ("../escape.png", "..\\escape.png", "C:\\temp\\escape.png"):
            with self.subTest(name=name):
                with self.assertRaises(EvidencePathError):
                    self.save_image(name=name)
        outside = self.root / "escape.png"
        self.assertFalse(outside.exists())
        self.assertEqual([p for p in self.evidence_root.rglob("*") if p.is_file()], [])

    def test_cross_tenant_cannot_read_or_list(self):
        record = self.save_image()
        with self.assertRaises(EvidenceNotFoundError):
            self.store.read(
                record["evidence_id"],
                tenant_id=OTHER_TENANT,
                store_id=STORE,
                task_id=TASK,
            )
        self.assertEqual(
            self.store.list_evidence(tenant_id=OTHER_TENANT, store_id=STORE, task_id=TASK),
            [],
        )

    def test_scope_requires_exact_store_and_task(self):
        record = self.save_image()
        with self.assertRaises(EvidenceNotFoundError):
            self.store.read(
                record["evidence_id"],
                tenant_id=TENANT,
                store_id="OTHER-STORE",
                task_id=TASK,
            )
        with self.assertRaises(EvidenceNotFoundError):
            self.store.read(
                record["evidence_id"],
                tenant_id=TENANT,
                store_id=STORE,
                task_id="OTHER-TASK",
            )
        self.assertEqual(
            self.store.list_evidence(tenant_id=TENANT, store_id=STORE, task_id="OTHER-TASK"),
            [],
        )

    def test_read_rejects_tampered_stored_path(self):
        record = self.save_image()
        with Database(self.db_path, tenant_id=TENANT).connection() as conn:
            conn.execute(
                "update evidence_files set stored_path=? where evidence_id=?",
                ("../outside.png", record["evidence_id"]),
            )
        with self.assertRaises(EvidencePathError):
            self.store.read(
                record["evidence_id"],
                tenant_id=TENANT,
                store_id=STORE,
                task_id=TASK,
            )


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 证据存储")
class PostgresEvidenceStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.mkdtemp(prefix="aiyg_evidence_pg_")
        cls.server = pgserver.get_server(Path(cls.temp) / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.temp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        self.root = Path(self.temp) / f"evidence-{self._testMethodName}"
        self.store = EvidenceStore(self.dsn, root_dir=self.root, tenant_id=TENANT)

    def test_save_read_and_scope_on_postgres(self):
        record = self.store.save(
            task_id=TASK,
            original_name="postgres.png",
            data=PNG,
            uploaded_by="employee01",
            store_id=STORE,
            tenant_id=TENANT,
        )
        self.assertEqual(
            self.store.read(
                record["evidence_id"],
                tenant_id=TENANT,
                store_id=STORE,
                task_id=TASK,
            ),
            PNG,
        )
        with self.assertRaises(EvidenceNotFoundError):
            self.store.read(
                record["evidence_id"],
                tenant_id=OTHER_TENANT,
                store_id=STORE,
                task_id=TASK,
            )
        self.assertEqual(
            self.store.list_evidence(tenant_id=OTHER_TENANT, store_id=STORE, task_id=TASK),
            [],
        )


if __name__ == "__main__":
    unittest.main()