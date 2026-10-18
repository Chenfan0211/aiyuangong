import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class EscapingRegressionTests(unittest.TestCase):
    def test_dynamic_pages_escape_html_metacharacters(self):
        for name in ("dashboard.html", "governance.html"):
            html = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("const esc=v=>String(v??'');", html, name)
            self.assertIn("&amp;", html, name)
            self.assertIn("&lt;", html, name)


if __name__ == "__main__":
    unittest.main()
