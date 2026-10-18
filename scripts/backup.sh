#!/usr/bin/env sh
set -eu

# 创建加密备份；默认写入受控本地卷，配置 S3 后自动切换到对象存储。
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN="${PYTHON_BIN:-python3}"
STORE_ID="${STORE_AGENT_STORE_ID:-WH-LION-001}"
TENANT_ID="${STORE_AGENT_TENANT_ID:-LPL-001}"
DB="${STORE_AGENT_STORE_DB:?missing STORE_AGENT_STORE_DB}"
EVIDENCE_DIR="${STORE_AGENT_EVIDENCE_DIR:-/app/evidence}"
CONFIG_PATH="${STORE_AGENT_CONFIG:-/app/store_config.json}"
BACKUP_STORAGE="${STORE_AGENT_BACKUP_STORAGE:-local}"
LOCAL_ROOT="${STORE_AGENT_BACKUP_LOCAL_ROOT:-/app/data/backups}"

cd "$ROOT_DIR"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

set -- backup --db "$DB" --tenant-id "$TENANT_ID" --store-id "$STORE_ID" --evidence-dir "$EVIDENCE_DIR" --config "$CONFIG_PATH" --storage "$BACKUP_STORAGE" --local-root "$LOCAL_ROOT"

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
