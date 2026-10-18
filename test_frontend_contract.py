import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAGES = ("agent.html", "dashboard.html", "governance.html", "settings.html")
PAGE_SCRIPTS = (
    ROOT / "static" / "agent.js",
    ROOT / "static" / "dashboard.js",
    ROOT / "static" / "governance.js",
    ROOT / "static" / "settings.js",
)


class FrontendContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_session_client_locks_cookie_and_csrf_names(self):
        source = self.read("static/session-client.js")
        self.assertIn("store_ops_session", source)
        self.assertIn("store_ops_csrf", source)
        self.assertIn("X-CSRF-Token", source)
        self.assertRegex(source, r"credentials\s*:\s*['\"]same-origin['\"]")
        self.assertRegex(source, r"SAFE_METHODS|SAFE_METHOD")
        self.assertRegex(source, r"status\s*===\s*401")

    def test_every_page_loads_shared_client_before_page_script(self):
        for page in PAGES:
            source = self.read(page)
            client_at = source.find("/static/session-client.js")
            page_script_at = source.find("/static/" + page.replace(".html", ".js"))
            self.assertGreaterEqual(client_at, 0, page)
            self.assertGreater(page_script_at, client_at, page)

    def test_browser_scripts_do_not_store_or_send_bearer_tokens(self):
        forbidden = (
            "store-agent-token",
            "Authorization",
            "Bearer ",
            "localStorage.setItem",
            "localStorage.getItem",
        )
        for path in PAGE_SCRIPTS:
            source = path.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, source, f"{path.name} still contains {marker}")

    def test_page_scripts_delegate_requests_to_shared_client(self):
        for path in PAGE_SCRIPTS:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("fetch(", source, path.name)
            self.assertIn("StoreSession", source, path.name)
            self.assertIn("createClient", source, path.name)

    def test_agent_has_mfa_and_expired_session_recovery(self):
        html = self.read("agent.html")
        source = self.read("static/agent.js")
        self.assertIn('id="mfaBox"', html)
        self.assertIn('id="mfaCode"', html)
        self.assertIn("/api/auth/mfa/verify", source)
        self.assertIn("登录已过期", source)
        self.assertIn("showLogin", source)

    def test_settings_patch_uses_revision_optimistic_lock(self):
        source = self.read("static/settings.js")
        self.assertIn("revision", source)
        self.assertIn("patchJson", source)

    def test_playwright_scaffold_and_required_scenarios_exist(self):
        package = self.read("package.json")
        config = self.read("playwright.config.js")
        self.assertIn('"@playwright/test"', package)
        self.assertIn("playwright test", package)
        self.assertIn("projects", config)
        self.assertIn("Desktop Chrome", config)
        self.assertIn("Pixel", config)

        e2e = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests" / "e2e").glob("*.js")))
        for marker in (
            "getByRole('button', { name: '登录'",
            "mfa",
            "filechooser",
            "verified",
            "settings",
            "logout",
            "expired",
            "localStorage",
        ):
            self.assertIn(marker, e2e)


if __name__ == "__main__":
    unittest.main()

