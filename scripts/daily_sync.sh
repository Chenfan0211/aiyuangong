#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN="${PYTHON_BIN:-python3}"
STORE_ID="${STORE_AGENT_STORE_ID:-WH-LION-001}"

cd "$ROOT_DIR"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/daily_sync.py" --store-id "$STORE_ID" "$@"
