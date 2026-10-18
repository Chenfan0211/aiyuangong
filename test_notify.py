# -*- coding: utf-8 -*-
"""批次 12：消息构建、载荷形态、outbox 草稿、真实 POST（本地桩）、发送账本幂等。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from governance import ApprovalStore
from notify import (
    DEFAULT_BASE_URL,
    Message,
    NotificationLedger,
    Notifier,
    build_approval_digest,
    build_daily_digest,
    build_overdue_digest,
    build_quality_alert,
    render_payload,
)
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from reporting import StoreReporter
from snapshot_builder import build_snapshot, load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
PACKAGE_ID = "MT-1632214010"


class StubServer:
    """本地桩：记录收到的请求，返回可配置的响应体。"""

    def __init__(self, response=None, status=200):
        self.requests = []
        self.response = response if response is not None else {"code": 0, "msg": "success"}
        self.status = status
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                stub.requests.append({"path": self.path, "body": json.loads(raw or "{}"),
                                      "content_type": self.headers.get("Content-Type")})
                payload = json.dumps(stub.response).encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):                       # 静音
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metrics_db, self.ops_db = self.root / "m.db", self.root / "s.db"
        self.config = dict(load_config(ROOT / "store_config.json"), tenant_id=TENANT)
        self.metrics = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)
        self.tasks = TaskStore(self.ops_db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.ops_db, tenant_id=TENANT)
        self.ledger = NotificationLedger(self.ops_db, tenant_id=TENANT)
        self.today = date.today()
        self.yesterday = self.today - timedelta(days=1)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, days: int = 8):
        for platform, base in (("meituan", 100), ("douyin", 80)):
            lines = []
            for offset in range(days, 0, -1):
                day = (self.today - timedelta(days=offset)).isoformat()
                lines.append(f"{day},{platform},{STORE},{base},{base // 5},4,3,300,0,1,0,4.8\n")
            path = self.root / f"{platform}.csv"
            path.write_bytes((HEADER + "".join(lines)).encode("utf-8"))
            self.metrics.import_csv(path, STORE)
        pkg = self.root / "p.csv"
        pkg.write_bytes((PKG + f"{self.today.isoformat()},meituan,{STORE},{PACKAGE_ID},AI全周小包2h,168,5,4,1200\n").encode("utf-8"))
        self.metrics.import_packages_csv(pkg, STORE)
        plan = generate_daily_plan(build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks))
        self.tasks.save_plan(plan, STORE)
        self.approvals.sync_from_plan(plan, STORE)
        return plan

    def reporter(self):
        return StoreReporter(self.config, self.metrics, self.tasks, self.approvals,
                             config_path=ROOT / "store_config.json")


class MessageTests(Base):
    def test_payload_shapes(self):
        message = Message("daily", "日报", "正文", "2026-10-18")
        self.assertEqual(render_payload("feishu", message),
                         {"msg_type": "text", "content": {"text": "正文"}})
        self.assertEqual(render_payload("wecom", message),
                         {"msgtype": "markdown", "markdown": {"content": "正文"}})
        with self.assertRaises(ValueError):
            render_payload("outbox", message)

    def test_daily_digest_content(self):
        self.seed()
        message = build_daily_digest(self.reporter())
        self.assertFalse(message.empty)
        self.assertEqual(message.kind, "daily")
        text = message.text
        for expected in ("经营日报", "曝光", "支付", "核销", "GMV", "待审批", "看板"):
            self.assertIn(expected, text)
        self.assertIn(f"{DEFAULT_BASE_URL}/dashboard", text)
        self.assertGreaterEqual(message.meta["pending_approvals"], 1)

    def test_daily_digest_without_data_is_empty(self):
        message = build_daily_digest(self.reporter())
        self.assertTrue(message.empty)
        self.assertIn("没有已校验的平台数据", message.text)

    def test_financial_fields_can_be_hidden(self):
        self.seed()
        with_finance = build_daily_digest(self.reporter()).text
        without = build_daily_digest(self.reporter(), include_financial=False).text
        self.assertIn("GMV", with_finance)
        self.assertNotIn("GMV", without)
        self.assertNotIn("退款率", without)

    def test_overdue_approval_and_quality_digests(self):
        plan = self.seed()
        overdue_source = next(t for t in plan["tasks"] if not t["requires_approval"])
        self.tasks.mark_overdue(STORE, moment="2099-01-01T00:00:00")
        overdue = build_overdue_digest(self.tasks, STORE)
        self.assertFalse(overdue.empty)
        self.assertIn("逾期任务", overdue.text)
        self.assertIn("负责人", overdue.text)

        approvals_message = build_approval_digest(self.approvals, STORE)
        self.assertFalse(approvals_message.empty)
        self.assertIn("待审批", approvals_message.text)
        self.assertIn("/governance", approvals_message.text)

        quality = build_quality_alert(self.metrics, STORE, self.config, days=30)
        self.assertEqual(quality.kind, "quality")

    def test_empty_digests_are_marked_empty(self):
        self.assertTrue(build_overdue_digest(self.tasks, STORE).empty)
        self.assertTrue(build_approval_digest(self.approvals, STORE).empty)
        # 有数据且完整时，质量提醒应为空消息（不打扰）
        self.seed()
        self.assertTrue(build_quality_alert(self.metrics, STORE, self.config, days=7).empty)

    def test_quality_alert_for_store_without_data_is_not_empty(self):
        """空库是"要行动"的状态：必须推一条提醒，而不是沉默。"""
        message = build_quality_alert(self.metrics, STORE, self.config, days=7)
        self.assertFalse(message.empty)
        self.assertIn("没有任何平台数据", message.text)


class SendTests(Base):
    def message(self, window="2026-10-18"):
        return Message("daily", "日报", "【武汉狮子座店】经营日报\n支付 10 单", window)

    def test_outbox_writes_draft_without_network(self):
        notifier = Notifier("outbox", outbox_dir=self.root / "outbox", ledger=self.ledger, store_id=STORE)
        result = notifier.send(self.message())
        self.assertTrue(result["drafted"])
        self.assertFalse(result["sent"])
        files = list((self.root / "outbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "daily")
        self.assertEqual(payload["window_key"], "2026-10-18")
        history = self.ledger.list(STORE)
        self.assertEqual((len(history), history[0]["status"]), (1, "drafted"))

    def test_feishu_send_posts_expected_payload(self):
        stub = StubServer()
        try:
            notifier = Notifier("feishu", webhook_url=stub.url, ledger=self.ledger, store_id=STORE)
            result = notifier.send(self.message())
            self.assertEqual(len(stub.requests), 1)
            body = stub.requests[0]["body"]
            self.assertEqual(body["msg_type"], "text")
            self.assertIn("经营日报", body["content"]["text"])
            self.assertIn("application/json", stub.requests[0]["content_type"])
            self.assertTrue(result["sent"])
            self.assertEqual(self.ledger.list(STORE)[0]["status"], "sent")
        finally:
            stub.stop()

    def test_wecom_payload_and_failure_status(self):
        stub = StubServer(response={"errcode": 93000, "errmsg": "invalid webhook"})
        try:
            notifier = Notifier("wecom", webhook_url=stub.url, ledger=self.ledger, store_id=STORE)
            result = notifier.send(self.message())
            self.assertFalse(result["sent"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(stub.requests[0]["body"]["msgtype"], "markdown")
            self.assertEqual(self.ledger.list(STORE)[0]["status"], "failed")
        finally:
            stub.stop()

    def test_http_error_status_is_failure(self):
        stub = StubServer(response={"code": 0}, status=500)
        try:
            notifier = Notifier("feishu", webhook_url=stub.url, ledger=self.ledger, store_id=STORE)
            result = notifier.send(self.message())
            self.assertFalse(result["sent"])
            self.assertEqual(result["http_status"], 500)
        finally:
            stub.stop()

    def test_success_is_not_resent(self):
        stub = StubServer()
        try:
            notifier = Notifier("feishu", webhook_url=stub.url, ledger=self.ledger, store_id=STORE)
            first = notifier.send(self.message())
            second = notifier.send(self.message())
            self.assertTrue(first["sent"])
            self.assertTrue(second["skipped"])
            self.assertEqual(second["reason"], "already_sent")
            self.assertEqual(len(stub.requests), 1)              # 没有重复推送
        finally:
            stub.stop()

    def test_failed_send_is_retried_and_force_resends(self):
        stub = StubServer(response={"code": 19001, "msg": "param invalid"})
        try:
            notifier = Notifier("feishu", webhook_url=stub.url, ledger=self.ledger, store_id=STORE)
            notifier.send(self.message())
            notifier.send(self.message())                        # 失败状态允许重试
            self.assertEqual(len(stub.requests), 2)
            stub.response = {"code": 0}
            result = notifier.send(self.message(), force=True)    # 成功后强制重发
            self.assertTrue(result["sent"])
            self.assertEqual(len(stub.requests), 3)
        finally:
            stub.stop()

    def test_empty_message_is_skipped_and_not_recorded(self):
        notifier = Notifier("outbox", outbox_dir=self.root / "outbox", ledger=self.ledger, store_id=STORE)
        result = notifier.send(Message("overdue", "逾期提醒", "没有逾期", "2026-10-18", empty=True))
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "empty")
        self.assertEqual(self.ledger.list(STORE), [])
        self.assertFalse((self.root / "outbox").exists())

    def test_channel_validation(self):
        with self.assertRaises(ValueError):
            Notifier("magic")
        with self.assertRaises(ValueError):
            Notifier("feishu")                                   # 缺 webhook


class CliTests(Base):
    def test_cli_notify_writes_outbox_and_records(self):
        self.seed()
        env = dict(os.environ, TZ="Asia/Shanghai")
        env.pop("FEISHU_WEBHOOK_URL", None)
        env.pop("WECOM_WEBHOOK_URL", None)
        result = subprocess.run(
            [sys.executable, "store_cli.py", "--metrics-db", str(self.metrics_db), "--store-db", str(self.ops_db),
             "notify", "--kind", "daily"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["drafted"])
        self.assertEqual(payload["channel"], "outbox")
        self.assertTrue(list((self.root / "outbox").glob("*.json")))
        self.assertIn("经营日报", payload["message"]["text"])

        history = subprocess.run(
            [sys.executable, "store_cli.py", "--store-db", str(self.ops_db), "notify-history"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(history.returncode, 0, history.stderr[-400:])
        self.assertEqual(len(json.loads(history.stdout)["history"]), 1)


if __name__ == "__main__":
    unittest.main()
