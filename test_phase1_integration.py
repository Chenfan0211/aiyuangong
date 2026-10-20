# -*- coding: utf-8 -*-
"""第一阶段 AI 店长闭环：绑定、入站消息、周期对比与归因。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from platform_data import PlatformMetricStore
from security import sign_webhook

ROOT = Path(__file__).resolve().parent
TENANT = "LPL-001"
STORE = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"
INBOUND_SECRET = "phase1-inbound-secret"


class Phase1AiStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "store_config.json"
        self.config.write_text((ROOT / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.ops_db = self.root / "ops.db"
        self.metrics_db = self.root / "metrics.db"
        self.chat_db = self.root / "chat.db"
        self.auth = AuthStore(self.ops_db, tenant_id=TENANT)
        self.owner = self.auth.create_user("owner01", PASSWORD, "店主老王", "owner", TENANT, STORE)
        self.manager = self.auth.create_user("manager01", PASSWORD, "店长小王", "manager", TENANT, STORE)
        self.staff = self.auth.create_user("staff01", PASSWORD, "店员小李", "employee", TENANT, STORE)
        self._seed_metrics()
        self._old_secret = os.environ.get("STORE_AGENT_INBOUND_SIGNING_SECRET")
        os.environ["STORE_AGENT_INBOUND_SIGNING_SECRET"] = INBOUND_SECRET
        self.app = create_app(
            config_path=self.config,
            metrics_db=self.metrics_db,
            store_db=self.ops_db,
            chat_db=self.chat_db,
            evidence_dir=self.root / "evidence",
        )
        self.client = TestClient(self.app)
        self.tokens = {
            "owner": self._login("owner01"),
            "manager": self._login("manager01"),
            "staff": self._login("staff01"),
        }
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        if self._old_secret is None:
            os.environ.pop("STORE_AGENT_INBOUND_SIGNING_SECRET", None)
        else:
            os.environ["STORE_AGENT_INBOUND_SIGNING_SECRET"] = self._old_secret
        self.temp.cleanup()

    def _login(self, username):
        response = self.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    @staticmethod
    def _headers(token):
        return {"Authorization": "Bearer " + token}

    def _seed_metrics(self):
        store = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)
        header = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                  "gmv,refunds,review_count,negative_review_count,avg_rating\n")
        for platform in ("meituan", "douyin"):
            lines = [header]
            base = 100 if platform == "meituan" else 80
            for offset in range(13, -1, -1):
                day = (date.today() - timedelta(days=offset)).isoformat()
                current = base + (13 - offset)
                previous = current - 10 if offset == 0 else current
                views = int(previous * 0.23)
                paid = max(1, int(previous * 0.06))
                verified = max(1, paid - (3 if offset == 0 else 1))
                gmv = paid * 120
                refunds = 3 if offset == 0 else 0
                reviews = 2
                negatives = 1 if offset == 0 else 0
                rating = 3.8 if offset == 0 else 4.8
                lines.append(f"{day},{platform},{STORE},{previous},{views},{paid},{verified},{gmv},{refunds},{reviews},{negatives},{rating}\n")
            path = self.root / f"{platform}.csv"
            path.write_text("".join(lines), encoding="utf-8")
            store.import_csv(path, STORE)

    def test_owner_binds_chat_identity_and_employee_cannot_read_bindings(self):
        denied = self.client.get("/api/admin/channel-bindings", headers=self._headers(self.tokens["staff"]))
        self.assertEqual(denied.status_code, 403, denied.text)

        created = self.client.post("/api/admin/channel-bindings", headers=self._headers(self.tokens["owner"]), json={
            "provider": "mock",
            "external_user_id": "ext-manager-1",
            "user_id": self.manager["user_id"],
            "display_name": "店长小王",
            "mobile": "13800138000",
        })
        self.assertEqual(created.status_code, 200, created.text)
        row = created.json()["binding"]
        self.assertEqual(row["external_user_id"], "ext-manager-1")
        self.assertEqual(row["mobile_masked"], "138****8000")
        self.assertNotIn("mobile", row)

        listed = self.client.get("/api/admin/channel-bindings", headers=self._headers(self.tokens["owner"]))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["bindings"]), 1)

    def test_signed_inbound_message_uses_bound_identity_and_returns_analysis(self):
        created = self.client.post("/api/admin/channel-bindings", headers=self._headers(self.tokens["owner"]), json={
            "provider": "mock",
            "external_user_id": "ext-manager-1",
            "user_id": self.manager["user_id"],
            "display_name": "店长小王",
            "mobile": "13800138000",
        })
        self.assertEqual(created.status_code, 200, created.text)

        now = int(time.time())
        body = json.dumps({
            "external_user_id": "ext-manager-1",
            "display_name": "店长小王",
            "mobile": "13800138000",
            "text": "对比昨天和前7天的数据，为什么订单下降？",
            "session_id": "inbound-session-1",
            "timestamp": now,
            "nonce": "inbound-nonce-1",
        }, ensure_ascii=False).encode("utf-8")
        signed = sign_webhook(INBOUND_SECRET, body, timestamp=now)
        response = self.client.post(
            "/api/inbound/messages/mock",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Store-Signature": signed["signature"],
                "X-Store-Timestamp": str(now),
                "X-Store-Nonce": "inbound-nonce-1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertTrue(payload["bound"])
        self.assertIn("对比", payload["reply"])
        self.assertIn("sources", payload)

        replay = self.client.post(
            "/api/inbound/messages/mock",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Store-Signature": signed["signature"],
                "X-Store-Timestamp": str(now),
                "X-Store-Nonce": "inbound-nonce-1",
            },
        )
        self.assertEqual(replay.status_code, 401, replay.text)

    def test_inbound_rejects_bad_signature_and_unknown_identity(self):
        now = int(time.time())
        body = json.dumps({
            "external_user_id": "unknown-user",
            "text": "看一下美团最近7天数据",
            "session_id": "inbound-session-2",
            "timestamp": now,
            "nonce": "inbound-nonce-2",
        }, ensure_ascii=False).encode("utf-8")
        signed = sign_webhook(INBOUND_SECRET, body, timestamp=now)
        bad = self.client.post(
            "/api/inbound/messages/mock",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Store-Signature": "bad-signature",
                "X-Store-Timestamp": str(now),
                "X-Store-Nonce": "inbound-nonce-2",
            },
        )
        self.assertEqual(bad.status_code, 401, bad.text)

        unknown = self.client.post(
            "/api/inbound/messages/mock",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Store-Signature": signed["signature"],
                "X-Store-Timestamp": str(now),
                "X-Store-Nonce": "inbound-nonce-2",
            },
        )
        self.assertEqual(unknown.status_code, 200, unknown.text)
        payload = unknown.json()
        self.assertFalse(payload["bound"])
        self.assertNotIn("sources", payload)

    def test_period_comparison_api_and_chat_include_dual_windows(self):
        headers = self._headers(self.tokens["manager"])
        response = self.client.get(
            "/api/analysis/compare?anchor=auto&window=day,week&platform=meituan",
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["store_id"], STORE)
        self.assertIn("day", payload["windows"])
        self.assertIn("week", payload["windows"])
        self.assertIn("meituan", payload["platforms"])

        chat = self.client.post(
            "/api/chat",
            headers=headers,
            json={"session_id": "phase1-chat-1", "message": "对比昨天和前7天数据，为什么订单下降？"},
        )
        self.assertEqual(chat.status_code, 200, chat.text)
        result = chat.json()
        self.assertIn(result["intent"], {"period_comparison", "store_diagnosis"})
        self.assertIn("对比", result["answer"])
        self.assertIn("依据", result["answer"])
        self.assertTrue(result["sources"])

    def test_analysis_strips_financial_fields_for_employee(self):
        response = self.client.get(
            "/api/analysis/compare?anchor=auto&window=week&platform=meituan",
            headers=self._headers(self.tokens["staff"]),
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        containers = []
        for window in payload["windows"].values():
            containers.append(window)
            containers.extend(window.get("platforms", {}).values())
        for item in containers:
            for field in ("metrics", "previous_metrics", "change_percent", "absolute_change"):
                values = item.get(field) or {}
                for key in ("gmv", "refunds", "refund_rate"):
                    self.assertNotIn(key, values)


if __name__ == "__main__":
    unittest.main()
