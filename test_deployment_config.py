import re
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


    def test_production_env_file_is_not_committed_or_baked_into_image(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn(".env.production", gitignore)
        self.assertIn(".env.production", dockerignore)


    @staticmethod
    def _service_block(compose, name):
        marker = f"\n  {name}:\n"
        start = compose.index(marker) + len(marker)
        next_service = re.search(r"\n  [a-z][a-z0-9_-]*:\n", compose[start:])
        end = start + next_service.start() if next_service else len(compose)
        return compose[start:end]

    def test_ops_service_runs_daily_sync_with_production_mounts(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        block = self._service_block(compose, "ops")
        self.assertIn('profiles: ["ops"]', block)
        self.assertIn('command: ["python", "scripts/daily_sync.py"]', block)
        for key in ("STORE_AGENT_METRICS_DB", "STORE_AGENT_STORE_DB", "STORE_AGENT_CHAT_DB"):
            self.assertIn(f"{key}: postgresql://", block)
        for volume in (
            "- backup_data:/app/data",
            "- evidence_data:/app/evidence:ro",
            "- imports_inbox:/app/imports/inbox:ro",
            "- ./store_config.json:/app/store_config.json:ro",
        ):
            self.assertIn(volume, block)
        self.assertIn("condition: service_healthy", block)

    def test_production_env_variants_are_ignored_but_example_is_kept(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for source in (gitignore, dockerignore):
            self.assertIn(".env.production.*", source)
            self.assertIn("!.env.production.example", source)
        self.assertIn(".env.production", gitignore)
        self.assertIn(".env.production", dockerignore)

    def test_production_template_contains_runtime_paths_and_base_url(self):
        template = (ROOT / ".env.production.example").read_text(encoding="utf-8")
        for name in (
            "STORE_AGENT_BASE_URL",
            "STORE_AGENT_IMPORT_INBOX",
            "STORE_AGENT_BACKUP_LOCAL_ROOT",
            "STORE_AGENT_OPS_LOCK",
        ):
            self.assertIn(f"\n{name}=", "\n" + template, name)


if __name__ == "__main__":
    unittest.main()
