# -*- coding: utf-8 -*-
"""第 8 期：工具模板/实例接口（导入 → 派生 → 沙箱调用 → 导出）。"""
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from tool_manifest import export_template_zip

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
MANIFEST = {"template_key": "self-check", "name": "连通性自检", "entrypoint": "echo"}


class ToolApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_db = root / "ops.db"
        auth = AuthStore(self.store_db, tenant_id=TENANT)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                            metrics_db=root / "metrics.db", store_db=self.store_db,
                                            chat_db=root / "chat.db", auth_mode="session"))
        self.tokens = {}
        for name in ("owner01", "manager01", "staff01"):
            status, data = self.call("POST", "/api/auth/login", {"username": name, "password": PASSWORD})
            self.assertEqual(status, 200, data)
            self.tokens[name] = data["token"]
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def call(self, method, path, payload=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, json=payload, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def raw(self, method, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.request(method, path, headers=headers)

    def test_permissions(self):
        status, _ = self.call("GET", "/api/tools")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/tools", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/tools", token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["templates"], [])

    def test_import_derive_invoke_export_chain(self):
        archive = export_template_zip(MANIFEST, secrets={"api_key": "plain-secret"})
        encoded = base64.b64encode(archive).decode("ascii")
        status, imported = self.call("POST", "/api/tool-templates/import",
                                     {"archive_base64": encoded}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, imported)
        self.assertEqual(imported["credentials"], {})          # 导出已清空 secret
        template_id = imported["template"]["template_id"]

        status, instance = self.call("POST", "/api/tool-instances",
                                     {"template_id": template_id, "instance_key": "inst-a",
                                      "credentials": {"api_key": "secret-a"}},
                                     token=self.tokens["manager01"])
        self.assertEqual(status, 200, instance)
        self.assertNotIn("secret-a", json.dumps(instance))

        status, invoked = self.call("POST", f"/api/tool-instances/{instance['instance_id']}/invoke",
                                    {"payload": {"probe": "ok"}}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, invoked)
        self.assertEqual(invoked["result"]["echo"], {"probe": "ok"})

        response = self.raw("GET", f"/api/tool-templates/export?template_id={template_id}",
                            token=self.tokens["manager01"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
            self.assertEqual(json.loads(zipped.read("secrets.json")), {})   # 不携带任何 secret
        self.assertNotIn(b"plain-secret", response.content)

    def test_network_policy_and_cross_store_rejections(self):
        archive = export_template_zip({**MANIFEST, "template_key": "net-tool", "network_required": True})
        encoded = base64.b64encode(archive).decode("ascii")
        _, imported = self.call("POST", "/api/tool-templates/import",
                                {"archive_base64": encoded}, token=self.tokens["owner01"])
        _, instance = self.call("POST", "/api/tool-instances",
                                {"template_id": imported["template"]["template_id"],
                                 "instance_key": "net-a"}, token=self.tokens["owner01"])
        status, body = self.call("POST", f"/api/tool-instances/{instance['instance_id']}/invoke",
                                 {"payload": {}}, token=self.tokens["owner01"])
        self.assertEqual(status, 409, body)
        self.assertIn("deny", str(body))

    def test_bad_base64_and_bad_manifest_are_rejected(self):
        status, body = self.call("POST", "/api/tool-templates/import",
                                 {"archive_base64": "!!!not-base64!!!"}, token=self.tokens["manager01"])
        self.assertEqual(status, 400, body)
        status, body = self.call("POST", "/api/tool-templates/import",
                                 {"archive_base64": base64.b64encode(b"not-a-zip").decode()},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()