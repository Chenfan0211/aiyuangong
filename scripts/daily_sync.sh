#!/usr/bin/env sh
set -eu

exec 9>/tmp/store_ops_daily.lock
flock -n 9 || { echo "daily sync already running"; exit 0; }

STORE_ID="${STORE_ID:-WH-LION-001}"
METRICS_DB="${STORE_AGENT_METRICS_DB:?missing STORE_AGENT_METRICS_DB}"
STORE_DB="${STORE_AGENT_STORE_DB:?missing STORE_AGENT_STORE_DB}"
CONFIG="${STORE_AGENT_CONFIG:-/app/store_config.json}"

python platform_sync.py --db "$METRICS_DB" --config "$CONFIG" --store-id "$STORE_ID" import-dir
if [ "$?" -ne 0 ]; then
  echo "import failed; stopping before planning" >&2
  exit 1
fi
python platform_sync.py --db "$METRICS_DB" --config "$CONFIG" --store-id "$STORE_ID" data-quality --days 30
python store_cli.py --config "$CONFIG" --metrics-db "$METRICS_DB" --store-db "$STORE_DB" plan --date today
python store_cli.py --config "$CONFIG" --store-db "$STORE_DB" notify --kind daily
python platform_sync.py --db "$METRICS_DB" --config "$CONFIG" --store-id "$STORE_ID" status
python platform_sync.py --db "$METRICS_DB" --config "$CONFIG" --store-id "$STORE_ID" summary --days 7
