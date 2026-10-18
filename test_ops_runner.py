# -*- coding: utf-8 -*-
"""统一运维任务存储与调度的 TDD 测试。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ops_runner import (
    DEFAULT_STEP_NAMES,
    OutboxAlerter,
    OpsRunner,
    Step,
)
from ops_store import OpsStore, OpsStoreError

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:  # pragma: no cover
    HAS_PG = False


class OpsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_run_lifecycle_records_json_and_finish_fields(self):
        store = OpsStore(self.db, tenant_id="tenant-1", store_id="WH-LION-001")
        run = store.start_run(
            "daily_sync", step_name="backup", attempt=1,
            details={"source": "test"},
        )
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["attempt"], 1)
        self.assertEqual(run["details"], {"source": "test"})
        self.assertIsNone(run["finished_at"])

        finished = store.finish_run(
            run["id"], status="success", details={"files": 2},
            alert_status="not_needed",
        )
        self.assertEqual(finished["status"], "success")
        self.assertEqual(finished["details"], {"files": 2})
        self.assertIsNone(finished["error"])
        self.assertIsNotNone(finished["finished_at"])
        self.assertEqual(finished["alert_status"], "not_needed")

        listed = store.list_runs(job_name="daily_sync")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], run["id"])

    def test_retry_request_public_lifecycle_records_source_step_and_attempt(self):
        store = OpsStore(self.db, tenant_id="tenant-1", store_id="WH-LION-001")
        source = store.record_run(
            "daily_sync", "import", status="failed", error="network down",
            details={"source": "test"},
        )

        queued = store.request_retry(source["id"], requested_by="owner-1")

        self.assertEqual(queued["source_run_id"], source["id"])
        self.assertEqual(queued["job_name"], "daily_sync")
        self.assertEqual(queued["step_name"], "import")
        self.assertEqual(queued["requested_by"], "owner-1")
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(queued["attempt"], 0)
        self.assertIsNone(queued["dead_letter_at"])

        claimed = store.claim_retry_request(queued["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempt"], 1)
        self.assertIsNotNone(claimed["started_at"])

        failed = store.finish_retry_request(
            queued["id"], success=False, error="still down",
        )
        self.assertEqual(failed["status"], "pending")
        self.assertEqual(failed["attempt"], 1)
        self.assertIn("still down", failed["last_error"])
        self.assertEqual(store.list_retry_requests(status="pending")[0]["id"], queued["id"])

    def test_retry_request_rejects_unknown_or_successful_source(self):
        store = OpsStore(self.db, tenant_id="tenant-1", store_id="WH-LION-001")
        with self.assertRaises(OpsStoreError):
            store.request_retry(9999, requested_by="owner-1")

        source = store.record_run("daily_sync", "backup", status="success")
        with self.assertRaises(OpsStoreError):
            store.request_retry(source["id"], requested_by="owner-1")


class OpsRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "runner.db"
        self.store = OpsStore(self.db, tenant_id="tenant-1", store_id="WH-LION-001")

    def tearDown(self):
        self.temp.cleanup()

    def test_step_retries_three_attempts_and_records_each_attempt(self):
        calls = []

        def flaky():
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise RuntimeError("temporary failure")
            return {"rows": 7}

        runner = OpsRunner(
            self.store,
            steps=[Step("import", flaky)],
            lock_path=self.root / "daily.lock",
        )
        report = runner.run(job_name="daily_sync")

        self.assertEqual(report["status"], "success")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(len(calls), 3)
        runs = self.store.list_runs(job_name="daily_sync")
        self.assertEqual([row["attempt"] for row in runs], [1, 2, 3])
        self.assertEqual([row["status"] for row in runs], ["failed", "failed", "success"])
        self.assertEqual(runs[-1]["details"], {"rows": 7})

    def test_exhausted_retries_alert_once_and_return_nonzero(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise RuntimeError("database unavailable")

        alerts = []

        class SpyAlerter:
            def send(self, event):
                alerts.append(event)
                return {"sent": True, "channel": "spy"}

        runner = OpsRunner(
            self.store,
            steps=[Step("quality", always_fails)],
            alerter=SpyAlerter(),
            lock_path=self.root / "daily.lock",
        )
        report = runner.run(job_name="daily_sync")

        self.assertEqual(report["status"], "failed")
        self.assertNotEqual(report["exit_code"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["step_name"], "quality")
        failed = self.store.list_runs(job_name="daily_sync")[-1]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempt"], 3)
        self.assertIn("database unavailable", failed["error"])
        self.assertEqual(failed["alert_status"], "sent")

    def test_exhausted_retries_without_webhook_records_outbox_status(self):
        def always_fails():
            raise RuntimeError("disk full")

        runner = OpsRunner(
            self.store,
            steps=[Step("backup", always_fails)],
        )
        report = runner.run(job_name="daily_sync")

        self.assertEqual(report["status"], "failed")
        failed = self.store.list_runs(job_name="daily_sync")[-1]
        self.assertEqual(failed["alert_status"], "outbox")
        self.assertEqual(len(list((self.root / "outbox").glob("ops-alert-*.json"))), 1)

    def test_outbox_is_default_when_no_webhook_channel_is_configured(self):
        outbox = self.root / "outbox"
        result = OutboxAlerter(outbox).send({
            "job_name": "daily_sync",
            "step_name": "backup",
            "status": "failed",
            "error": "disk full",
        })
        self.assertTrue(result["sent"])
        files = list(outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["step_name"], "backup")
        self.assertIn("disk full", payload["error"])

    def test_overlapping_run_exits_without_executing_steps(self):
        started = threading.Event()
        release = threading.Event()
        first_report = {}

        def blocking():
            started.set()
            release.wait(timeout=5)
            return {"ok": True}

        first = OpsRunner(
            self.store,
            steps=[Step("backup", blocking)],
            lock_path=self.root / "daily.lock",
        )
        thread = threading.Thread(target=lambda: first_report.update(first.run(job_name="daily_sync")))
        thread.start()
        self.assertTrue(started.wait(timeout=5))

        second = OpsRunner(
            self.store,
            steps=[Step("backup", lambda: self.fail("overlapping step must not execute"))],
            lock_path=self.root / "daily.lock",
        )
        overlapping = second.run(job_name="daily_sync")
        self.assertTrue(overlapping["skipped"])
        self.assertEqual(overlapping["exit_code"], 0)
        self.assertEqual(overlapping["steps"], [])

        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_report["status"], "success")

    def test_fixed_daily_step_order_is_public(self):
        self.assertEqual(
            DEFAULT_STEP_NAMES,
            ("backup", "import", "data-quality", "plan", "notify", "summary"),
        )

    def test_retry_request_executes_only_configured_step_and_records_success(self):
        source = self.store.record_run("daily_sync", "import", status="failed", error="network")
        queued = self.store.request_retry(source["id"], requested_by="owner-1")
        calls = []

        def import_action():
            calls.append("import")
            return {"rows": 3}

        runner = OpsRunner(
            self.store,
            steps=[Step("import", import_action)],
            lock_path=self.root / "daily.lock",
        )
        report = runner.consume_retries(job_name="daily_sync")

        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(calls, ["import"])
        retry = self.store.get_retry_request(queued["id"])
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["attempt"], 1)
        self.assertIsNone(retry["dead_letter_at"])
        retry_runs = [
            row for row in self.store.list_runs(job_name="daily_sync")
            if row["details"].get("retry_request_id") == queued["id"]
        ]
        self.assertEqual(len(retry_runs), 1)
        self.assertEqual(retry_runs[0]["step_name"], "import")
        self.assertEqual(retry_runs[0]["details"]["source_run_id"], source["id"])

    def test_retry_request_for_unconfigured_step_dead_letters_without_action(self):
        source = self.store.record_run(
            "daily_sync", "data-quality", status="failed", error="missing data",
        )
        queued = self.store.request_retry(source["id"], requested_by="owner-1")

        runner = OpsRunner(
            self.store,
            steps=[Step("import", lambda: self.fail("retry must not run arbitrary action"))],
            lock_path=self.root / "daily.lock",
        )
        report = runner.consume_retries(job_name="daily_sync")

        self.assertNotEqual(report["exit_code"], 0)
        self.assertEqual(report["dead_letters"], [queued["id"]])
        retry = self.store.get_retry_request(queued["id"])
        self.assertEqual(retry["status"], "dead_letter")
        self.assertEqual(retry["attempt"], 1)
        self.assertIsNotNone(retry["dead_letter_at"])
        self.assertIn("未配置", retry["last_error"])

    def test_retry_request_dead_letters_after_attempt_limit_and_alerts(self):
        source = self.store.record_run("daily_sync", "backup", status="failed", error="disk full")
        queued = self.store.request_retry(source["id"], requested_by="owner-1", max_attempts=2)
        calls = []
        alerts = []

        class SpyAlerter:
            def send(self, event):
                alerts.append(event)
                return {"sent": True, "channel": "spy"}

        def failing_backup():
            calls.append(1)
            raise RuntimeError("disk still full")

        runner = OpsRunner(
            self.store,
            steps=[Step("backup", failing_backup)],
            alerter=SpyAlerter(),
            lock_path=self.root / "daily.lock",
        )

        first = runner.consume_retries(job_name="daily_sync")
        first_retry = self.store.get_retry_request(queued["id"])
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(first_retry["status"], "pending")
        self.assertEqual(first_retry["attempt"], 1)

        second = runner.consume_retries(job_name="daily_sync")
        second_retry = self.store.get_retry_request(queued["id"])

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(second["exit_code"], 0)
        self.assertEqual(second["dead_letters"], [queued["id"]])
        self.assertEqual(second_retry["status"], "dead_letter")
        self.assertEqual(second_retry["attempt"], 2)
        self.assertIsNotNone(second_retry["dead_letter_at"])
        self.assertEqual(alerts[0]["status"], "dead_letter")
        self.assertEqual(alerts[0]["retry_request_id"], queued["id"])

    def test_run_consumes_pending_retry_before_regular_steps(self):
        source = self.store.record_run("daily_sync", "import", status="failed", error="network")
        self.store.request_retry(source["id"], requested_by="owner-1")
        calls = []

        def import_action():
            calls.append("call")
            return {"ok": True}

        runner = OpsRunner(
            self.store,
            steps=[Step("import", import_action)],
            lock_path=self.root / "daily.lock",
        )
        report = runner.run(job_name="daily_sync")

        self.assertEqual(report["status"], "success")
        self.assertEqual(len(report["retries"]), 1)
        self.assertEqual(report["retries"][0]["status"], "succeeded")
        self.assertEqual(calls, ["call", "call"])


class DailySyncScriptTests(unittest.TestCase):
    def test_script_exposes_fixed_order_and_four_notification_kinds(self):
        from scripts.daily_sync import NOTIFY_MESSAGE_KINDS, build_default_steps

        self.assertEqual(
            NOTIFY_MESSAGE_KINDS,
            ("daily", "overdue", "approvals", "quality"),
        )
        args = SimpleNamespace(
            python=sys.executable,
            config=Path("store_config.json"),
            metrics_db="data/platform_metrics.db",
            store_db="data/store_ops.db",
            store_id="WH-LION-001",
            inbox=Path("imports/inbox"),
            backup_dir=Path("data/backup"),
            days=30,
            allow_degraded=False,
            include_financial=False,
            base_url="http://127.0.0.1:8766",
        )
        self.assertEqual(
            tuple(step.name for step in build_default_steps(args)),
            DEFAULT_STEP_NAMES,
        )

    def test_backup_commands_include_daily_and_monthly_retention_prune(self):
        from scripts.daily_sync import build_backup_commands

        args = SimpleNamespace(
            python=sys.executable,
            config=Path("store_config.json"),
            metrics_db="data/platform_metrics.db",
            store_db="data/store_ops.db",
            tenant_id="tenant-1",
            store_id="WH-LION-001",
            inbox=Path("imports/inbox"),
            backup_dir=Path("data/backup"),
            days=30,
            allow_degraded=False,
            include_financial=False,
            base_url="http://127.0.0.1:8766",
        )

        commands = [list(command) for command in build_backup_commands(args)]
        prune_daily = [
            command for command in commands
            if "prune" in command and "daily" in command and "30" in command
        ]
        prune_monthly = [
            command for command in commands
            if "prune" in command and "monthly" in command and "12" in command
        ]

        self.assertEqual(len(commands), 3)
        self.assertEqual(len(prune_daily), 1)
        self.assertEqual(len(prune_monthly), 1)
        self.assertIn("backup_manager.py", commands[0][1])


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL advisory lock")
class PostgresAdvisoryLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_ops_lock_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_advisory_lock_blocks_second_holder_and_can_be_reacquired(self):
        from ops_runner import PostgresAdvisoryLock

        first = PostgresAdvisoryLock(self.dsn, "daily_sync", "WH-LION-001")
        second = PostgresAdvisoryLock(self.dsn, "daily_sync", "WH-LION-001")

        with first.hold() as acquired:
            self.assertTrue(acquired)
            with second.hold() as second_acquired:
                self.assertFalse(second_acquired)
        with first.hold() as reacquired:
            self.assertTrue(reacquired)


if __name__ == "__main__":
    unittest.main()
