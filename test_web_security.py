import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app_fastapi


ROOT = Path(__file__).resolve().parent


class EscapingRegressionTests(unittest.TestCase):
    def test_dynamic_pages_escape_html_metacharacters(self):
        for name in ("dashboard", "governance"):
            html = (ROOT / "static" / f"{name}.js").read_text(encoding="utf-8")
            self.assertNotIn("const esc=v=>String(v??'');", html, name)
            self.assertIn("&amp;", html, name)
            self.assertIn("&lt;", html, name)


class StaticAssetAndHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = app_fastapi.create_app(
            config_path=ROOT / "store_config.json",
            metrics_db=root / "metrics.db",
            store_db=root / "store.db",
            chat_db=root / "chat.db",
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_do_not_embed_scripts_or_styles(self):
        for path in ("/", "/dashboard", "/governance", "/index.html"):
            html = self.client.get(path).text
            self.assertNotIn("<style>", html, path)
            self.assertNotIn("<script>", html, path)
            self.assertIn("/static/", html, path)

    def test_static_assets_are_served(self):
        for name in ("agent.css", "agent.js", "dashboard.css", "dashboard.js",
                     "governance.css", "governance.js", "index.css", "index.js"):
            response = self.client.get("/static/" + name)
            self.assertEqual(response.status_code, 200, name)
            self.assertTrue(response.content, name)

    def test_security_headers_are_present(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        csp = response.headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)


if __name__ == "__main__":
    unittest.main()
