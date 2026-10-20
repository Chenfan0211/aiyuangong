# -*- coding: utf-8 -*-
"""生产第三方集成配置的统一预检。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_fastapi import create_app
from security import check_integration_config, gate_report
from scripts.daily_sync import build_parser as build_daily_sync_parser
from scripts.daily_sync import main as daily_sync_main


class IntegrationConfigTests(unittest.TestCase):

    @staticmethod
    def _production_env(**overrides):
        values = {
            "STORE_AGENT_ENV": "production",
            "STORE_AGENT_MFA_ENFORCE": "1",
            "STORE_AGENT_MFA_KEY": "mfa-" + "x" * 48,
            "POSTGRES_PASSWORD": "db-" + "p" * 40,
            "DOMAIN": "ops.shizizuo.cn",
            "ACME_EMAIL": "ops@shizizuo.cn",
            "STORE_AGENT_BASE_URL": "https://ops.shizizuo.cn",
            "STORE_AGENT_EVIDENCE_DIR": "/app/evidence",
            "STORE_AGENT_IMPORT_INBOX": "/app/imports/inbox",
            "STORE_AGENT_BACKUP_LOCAL_ROOT": "/app/data/backups",
            "STORE_AGENT_OPS_LOCK": "/app/data/ops/daily_sync.lock",
        }
        values.update(overrides)
        return values
    def test_defaults_use_mock_outbox_and_local_backup(self):
        result = check_integration_config({})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["selected"]["approval_callback_provider"], "mock")
        self.assertEqual(result["selected"]["alert_channel"], "outbox")
        self.assertEqual(result["selected"]["backup_storage"], "local")

    def test_feishu_callback_requires_verification_and_encrypt_keys(self):
        result = check_integration_config({
            "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER": "feishu",
        })
        self.assertFalse(result["ok"])
        self.assertTrue(any("FEISHU_VERIFICATION_TOKEN" in item for item in result["problems"]))
        self.assertTrue(any("FEISHU_ENCRYPT_KEY" in item for item in result["problems"]))

    def test_feishu_callback_passes_with_required_keys(self):
        result = check_integration_config({
            "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER": "feishu",
            "FEISHU_VERIFICATION_TOKEN": "verify-token",
            "FEISHU_ENCRYPT_KEY": "encrypt-key",
        })
        self.assertTrue(result["ok"], result)

    def test_wecom_callback_requires_token_aes_key_and_corp_id(self):
        result = check_integration_config({
            "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER": "wecom",
        })
        self.assertFalse(result["ok"])
        for name in ("WECOM_CALLBACK_TOKEN", "WECOM_ENCODING_AES_KEY", "WECOM_CORP_ID"):
            self.assertTrue(any(name in item for item in result["problems"]), name)

    def test_wecom_callback_passes_with_required_keys(self):
        result = check_integration_config({
            "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER": "wecom",
            "WECOM_CALLBACK_TOKEN": "callback-token",
            "WECOM_ENCODING_AES_KEY": "a" * 43,
            "WECOM_CORP_ID": "corp-001",
        })
        self.assertTrue(result["ok"], result)

    def test_real_alert_channel_requires_matching_webhook(self):
        result = check_integration_config({"STORE_AGENT_ALERT_CHANNEL": "wecom"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("WECOM_WEBHOOK_URL" in item for item in result["problems"]))
        complete = check_integration_config({
            "STORE_AGENT_ALERT_CHANNEL": "wecom",
            "WECOM_WEBHOOK_URL": "https://example.invalid/wecom",
        })
        self.assertTrue(complete["ok"], complete)

    def test_production_mfa_rejects_missing_or_placeholder_key(self):
        missing = check_integration_config(self._production_env(STORE_AGENT_MFA_KEY=""))
        self.assertFalse(missing["ok"])
        placeholder = check_integration_config(self._production_env(
            STORE_AGENT_MFA_KEY="replace-with-long-random-mfa-encryption-key",
        ))
        self.assertFalse(placeholder["ok"])
        strong = check_integration_config(self._production_env())
        self.assertTrue(strong["ok"], strong)

    def test_production_requires_public_base_url(self):
        missing = check_integration_config(self._production_env(STORE_AGENT_BASE_URL=""))
        self.assertFalse(missing["ok"])
        self.assertTrue(any("STORE_AGENT_BASE_URL" in item for item in missing["problems"]))
        for value in ("http://ops.shizizuo.cn", "https://127.0.0.1:8766", "https://ops.example.com"):
            with self.subTest(value=value):
                result = check_integration_config(self._production_env(STORE_AGENT_BASE_URL=value))
                self.assertFalse(result["ok"])
                self.assertTrue(any("STORE_AGENT_BASE_URL" in item for item in result["problems"]), result)

    def test_production_rejects_placeholder_database_domain_and_email(self):
        result = check_integration_config(self._production_env(
            POSTGRES_PASSWORD="replace-with-long-random-password",
            DOMAIN="ops.example.com",
            ACME_EMAIL="admin@example.com",
        ))
        self.assertFalse(result["ok"])
        for name in ("POSTGRES_PASSWORD", "DOMAIN", "ACME_EMAIL"):
            self.assertTrue(any(name in item for item in result["problems"]), name)

    def test_production_requires_container_runtime_paths(self):
        for name in (
            "STORE_AGENT_EVIDENCE_DIR",
            "STORE_AGENT_IMPORT_INBOX",
            "STORE_AGENT_BACKUP_LOCAL_ROOT",
            "STORE_AGENT_OPS_LOCK",
        ):
            with self.subTest(name=name):
                result = check_integration_config(self._production_env(**{name: ""}))
                self.assertFalse(result["ok"])
                self.assertTrue(any(name in item for item in result["problems"]), result)

    def test_s3_backup_requires_bucket_credentials_and_encryption_key(self):
        result = check_integration_config({"STORE_AGENT_BACKUP_STORAGE": "s3"})
        self.assertFalse(result["ok"])
        for name in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            self.assertTrue(any(name in item for item in result["problems"]), name)
        complete = check_integration_config({
            "STORE_AGENT_BACKUP_STORAGE": "s3",
            "S3_BUCKET": "backup-bucket",
            "S3_ACCESS_KEY_ID": "access-key",
            "S3_SECRET_ACCESS_KEY": "secret-key",
            "BACKUP_ENCRYPTION_KEY": "backup-" + "y" * 32,
        })
        self.assertTrue(complete["ok"], complete)


    def test_evidence_dir_env_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = root / "store_config.json"
            cfg.write_text(
                (Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            canonical = root / "canonical-evidence"
            legacy = root / "legacy-evidence"
            with patch.dict(os.environ, {"STORE_AGENT_EVIDENCE_DIR": str(canonical), "EVIDENCE_ROOT_DIR": str(legacy)}, clear=True):
                app = create_app(config_path=cfg, metrics_db=root / "metrics.db", store_db=root / "ops.db", chat_db=root / "chat.db")
                self.assertEqual(app.state.ctx.evidence.root_dir, canonical.resolve())
            with patch.dict(os.environ, {"EVIDENCE_ROOT_DIR": str(legacy)}, clear=True):
                app = create_app(config_path=cfg, metrics_db=root / "metrics.db", store_db=root / "ops.db", chat_db=root / "chat.db")
                self.assertEqual(app.state.ctx.evidence.root_dir, legacy.resolve())

    def test_app_cli_accepts_evidence_dir(self):
        from app_fastapi import build_parser

        expected = Path("custom-evidence")
        args = build_parser().parse_args(["--evidence-dir", str(expected)])
        self.assertEqual(args.evidence_dir, expected)

    def test_daily_sync_path_env_priority(self):
        canonical = Path("canonical-backup")
        legacy = Path("legacy-backup")
        with patch.dict(os.environ, {"STORE_AGENT_BACKUP_LOCAL_ROOT": str(canonical), "STORE_AGENT_BACKUP_DIR": str(legacy)}, clear=True):
            self.assertEqual(build_daily_sync_parser().parse_args(["--dry-run"]).backup_dir, canonical)
        with patch.dict(os.environ, {"STORE_AGENT_BACKUP_DIR": str(legacy)}, clear=True):
            self.assertEqual(build_daily_sync_parser().parse_args(["--dry-run"]).backup_dir, legacy)
        canonical_evidence = Path("canonical-evidence")
        legacy_evidence = Path("legacy-evidence")
        with patch.dict(os.environ, {"STORE_AGENT_EVIDENCE_DIR": str(canonical_evidence), "EVIDENCE_ROOT_DIR": str(legacy_evidence)}, clear=True):
            self.assertEqual(build_daily_sync_parser().parse_args(["--dry-run"]).evidence_dir, canonical_evidence)
        with patch.dict(os.environ, {"EVIDENCE_ROOT_DIR": str(legacy_evidence)}, clear=True):
            self.assertEqual(build_daily_sync_parser().parse_args(["--dry-run"]).evidence_dir, legacy_evidence)

    @patch.dict(os.environ, {"STORE_AGENT_ALERT_CHANNEL": "wecom"}, clear=False)
    def test_daily_sync_rejects_incomplete_real_alert_channel(self):
        os.environ.pop("WECOM_WEBHOOK_URL", None)
        os.environ.pop("STORE_AGENT_WECOM_WEBHOOK_URL", None)
        self.assertEqual(daily_sync_main(["--dry-run"]), 2)

    @patch.dict(os.environ, {
        "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER": "feishu",
        "FEISHU_VERIFICATION_TOKEN": "",
        "FEISHU_ENCRYPT_KEY": "",
    }, clear=False)
    def test_create_app_rejects_incomplete_real_callback_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "store_config.json"
            config.write_text(
                (Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "FEISHU_VERIFICATION_TOKEN"):
                create_app(
                    config_path=config,
                    metrics_db=root / "metrics.db",
                    store_db=root / "ops.db",
                    chat_db=root / "chat.db",
                    evidence_dir=root / "evidence",
                )

    def test_gate_report_includes_integration_configuration(self):
        report = gate_report(Path("."))
        gates = {item["gate"]: item for item in report["items"]}
        self.assertIn("生产集成配置已闭合", gates)


if __name__ == "__main__":
    unittest.main()