# -*- coding: utf-8 -*-
"""批次 11：看板与周/月报接口在两套传输层上的一致性测试。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_server import AgentHandler
from agent_tools import StoreTools
from auth import AuthStore
from governance import ApprovalStore, AuditCenter, ToolRunLog
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from reporting import StoreReporter
from snapshot_builder import build_snapshot, load_config
from store_agent import AgentSessionStore, StoreOperationsAgent
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"


class ReportingApiBase:
    transport = "stdlib"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        self.metrics_db, self.store_db, self.chat_db = root / "m.db", root / "s.db", root / "c.db"
        self.metrics = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)
        self.tasks = TaskStore(self.store_db, tenant_id=TENANT)
        self.auth = AuthStore(self.store_db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.store_db, tenant_id=TENANT)
        self.tool_log = ToolRunLog(self.store_db, tenant_id=TENANT)
        self.center = AuditCenter(self.store_db, tenant_id=TENANT)
        tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        self.today = date.today()

        for platform, base in (("meituan", 100), ("douyin", 80)):
            lines = []
            for offset in range(20, 0, -1):
                day = (self.today - timedelta(days=offset)).isoformat()
                lines.append(f"{day},{platform},{STORE},{base},{base//5},4,3,300,0,1,0,4.8\n")
            path = root / f"{platform}.csv"
            path.write_bytes((HEADER + "".join(lines)).encode("utf-8"))
            self.metrics.import_csv(path, STORE)
        pkg = root / "p.csv"
        pkg.write_bytes((PKG + f"{self.today.isoformat()},meituan,{STORE},MT-1632214010,AI全周小包2h,168,5,4,1200\n").encode("utf-8"))
        self.metrics.import_packages_csv(pkg, STORE)
        plan = generate_daily_plan(build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks))
        self.tasks.save_plan(plan, STORE)
        self.approvals.sync_from_plan(plan, STORE)

        for username, display, role in (("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)

        if self.transport == "fastapi":
            from fastapi.testclient import TestClient
            from app_fastapi import create_app
            self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                                metrics_db=self.metrics_db, store_db=self.store_db,
                                                chat_db=self.chat_db, auth_mode="session"))
        else:
            AgentHandler.agent = StoreOperationsAgent.from_tools(self.config, tools)
            AgentHandler.tools = tools
            AgentHandler.task_store = self.tasks
            AgentHandler.auth = self.auth
            AgentHandler.approvals = self.approvals
            AgentHandler.tool_log = self.tool_log
            AgentHandler.audit_center = self.center
            AgentHandler.reporter = StoreReporter(self.config, self.metrics, self.tasks, self.approvals,
                                                  config_path=ROOT / "store_config.json")
            AgentHandler.auth_mode = "session"
            AgentHandler.sessions = AgentSessionStore(self.chat_db, tenant_id=TENANT)
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
            self.port = self.server.server_address[1]
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

        self.tokens = {name: self.login(name) for name in ("manager01", "staff01")}
        if hasattr(self, "client"):
            self.client.cookies.clear()

    def tearDown(self):
        if self.transport == "fastapi":
            self.client.close()
        else:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, payload=None, token=None):
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.transport == "fastapi":
            response = self.client.request(method, path, json=payload, headers=headers)
            return response.status_code, response.text
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def call(self, method, path, payload=None, token=None):
        status, body = self._request(method, path, payload, token)
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, body

    def login(self, username, password=PASSWORD):
        status, data = self.call("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(status, 200, data)
        return data["token"]

    def test_dashboard_requires_token_and_respects_financial_permission(self):
        status, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(status, 401)
        status, manager = self.call("GET", "/api/dashboard?days=7", token=self.tokens["manager01"])
        self.assertEqual(status, 200, manager)
        self.assertTrue(manager["financial_included"])
        self.assertIn("gmv", manager["kpis"])
        self.assertGreater(len(manager["trend"]), 0)

        status, staff = self.call("GET", "/api/dashboard?days=7", token=self.tokens["staff01"])
        self.assertEqual(status, 200, staff)
        self.assertFalse(staff["financial_included"])
        for field in ("gmv", "refunds", "refund_rate"):
            self.assertNotIn(field, staff["kpis"])
        self.assertTrue(all("gmv" not in point for point in staff["trend"]))

    def test_weekly_and_monthly_require_report_permission(self):
        status, _ = self.call("GET", "/api/reports/weekly", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, week = self.call("GET", "/api/reports/weekly", token=self.tokens["manager01"])
        self.assertEqual(status, 200, week)
        self.assertIn("一、结论摘要", week["report"])
        status, month = self.call("GET", "/api/reports/monthly", token=self.tokens["manager01"])
        self.assertEqual(status, 200, month)
        self.assertIn("与上月相比", month["report"])
        self.assertEqual(list(month["period"].keys()), ["start", "end"])

    def test_tool_runs_record_dashboard_and_reports(self):
        self.call("GET", "/api/dashboard?days=7", token=self.tokens["manager01"])
        self.call("GET", "/api/reports/weekly", token=self.tokens["manager01"])
        status, body = self.call("GET", "/api/tool-runs?limit=20", token=self.tokens["manager01"])
        self.assertEqual(status, 403)          # 店长没有 audit:read
        runs = [r["tool_name"] for r in self.tool_log.list(STORE, limit=20)]
        self.assertIn("api:dashboard", runs)
        self.assertIn("api:report_weekly", runs)

    def test_dashboard_page_is_served(self):
        status, html = self._request("GET", "/dashboard")
        self.assertEqual(status, 200)
        for marker in ('id="kpis"', 'id="trend"', 'id="packages"', 'id="report"'):
            self.assertIn(marker, html)


class ReportingApiStdlibTests(ReportingApiBase, unittest.TestCase):
    transport = "stdlib"


class ReportingApiFastApiTests(ReportingApiBase, unittest.TestCase):
    transport = "fastapi"


if __name__ == "__main__":
    unittest.main()
