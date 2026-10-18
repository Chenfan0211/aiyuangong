import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class DeploymentFilesTests(unittest.TestCase):
    def test_container_and_proxy_contract(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("python:3.12-slim", dockerfile)
        self.assertIn("USER mallops", dockerfile)
        for service in ("db:", "app:", "caddy:"):
            self.assertIn(service, compose)
        self.assertIn("reverse_proxy app:8766", caddy)
        self.assertNotIn('\n      - "8766:', compose)
        self.assertNotIn('\n      - "5432:', compose)


if __name__ == "__main__":
    unittest.main()
