#!/usr/bin/env sh
set -eu

# 恢复到全新数据库；默认拒绝覆盖生产库，必须显式提供备份对象 key。
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN="${PYTHON_BIN:-python3}"
STORE_ID="${STORE_AGENT_STORE_ID:-WH-LION-001}"
TENANT_ID="${STORE_AGENT_TENANT_ID:-LPL-001}"
BACKUP_OBJECT_KEY="${BACKUP_OBJECT_KEY:?missing BACKUP_OBJECT_KEY}"
TARGET_DB="${RESTORE_DB_TARGET:?missing RESTORE_DB_TARGET}"
PRODUCTION_DB="${STORE_AGENT_STORE_DB:-}"
EVIDENCE_DIR="${RESTORE_EVIDENCE_DIR:-/app/restore/evidence}"
CONFIG_OUT="${RESTORE_CONFIG_OUT:-/app/restore/store_config.json}"
BACKUP_STORAGE="${STORE_AGENT_BACKUP_STORAGE:-local}"
LOCAL_ROOT="${STORE_AGENT_BACKUP_LOCAL_ROOT:-/app/data/backups}"

if [ "${RESTORE_CONFIRM_FRESH:-0}" != "1" ]; then
  echo "refusing restore: set RESTORE_CONFIRM_FRESH=1 after creating a fresh target database" >&2
  exit 2
fi

if [ "$TARGET_DB" = "$PRODUCTION_DB" ]; then
  echo "refusing restore: target equals production STORE_AGENT_STORE_DB" >&2
  exit 2
fi

cd "$ROOT_DIR"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

set -- restore --object-key "$BACKUP_OBJECT_KEY" --target-db "$TARGET_DB" --tenant-id "$TENANT_ID" --store-id "$STORE_ID" --evidence-dir "$EVIDENCE_DIR" --config-out "$CONFIG_OUT" --storage "$BACKUP_STORAGE" --local-root "$LOCAL_ROOT"

if [ -n "${BACKUP_EXPECTED_SHA256:-}" ]; then
  set -- "$@" --expected-sha256 "$BACKUP_EXPECTED_SHA256"
fi
if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
  set -- "$@" --encryption-key "$BACKUP_ENCRYPTION_KEY"
else
  set -- "$@" --encryption-key-file "${BACKUP_ENCRYPTION_KEY_FILE:-$LOCAL_ROOT/.backup-key}"
fi
if [ -n "${S3_BUCKET:-}" ]; then
  set -- "$@" --s3-bucket "$S3_BUCKET"
fi
if [ -n "${S3_PREFIX:-}" ]; then
  set -- "$@" --s3-prefix "$S3_PREFIX"
fi

exec "$PYTHON_BIN" "$ROOT_DIR/backup_manager.py" "$@"
