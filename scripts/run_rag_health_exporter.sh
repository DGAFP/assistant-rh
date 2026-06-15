#!/bin/sh
set -eu

: "${RAG_HEALTH_POSTGRES_DSN:?RAG_HEALTH_POSTGRES_DSN is required}"
: "${COCKPIT_METRICS_PUSH_URL:?COCKPIT_METRICS_PUSH_URL is required}"
: "${COCKPIT_TOKEN_SECRET_KEY:?COCKPIT_TOKEN_SECRET_KEY is required}"

RAG_HEALTH_EXPORTER_PORT="${RAG_HEALTH_EXPORTER_PORT:-9108}"
DB_HEALTH_POLL_INTERVAL_SECONDS="${DB_HEALTH_POLL_INTERVAL_SECONDS:-300}"
RAG_HEALTH_ENV_LABEL="${RAG_HEALTH_ENV_LABEL:-${APP_SCALEWAY_ENV:-${APP_ENV:-}}}"

if [ "$RAG_HEALTH_ENV_LABEL" = "production" ]; then
  RAG_HEALTH_ENV_LABEL="prod"
fi

if [ -z "$RAG_HEALTH_ENV_LABEL" ]; then
  echo "RAG_HEALTH_ENV_LABEL or APP_SCALEWAY_ENV is required" >&2
  exit 1
fi

export RAG_HEALTH_EXPORTER_PORT
export DB_HEALTH_POLL_INTERVAL_SECONDS
export RAG_HEALTH_ENV_LABEL

python - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

template = Path("config/grafana-alloy/rag-health.alloy.template").read_text(encoding="utf-8")
replacements = {
    "${RAG_HEALTH_EXPORTER_PORT}": os.environ["RAG_HEALTH_EXPORTER_PORT"],
    "${COCKPIT_METRICS_PUSH_URL_JSON}": json.dumps(os.environ["COCKPIT_METRICS_PUSH_URL"]),
    "${COCKPIT_TOKEN_SECRET_KEY_JSON}": json.dumps(os.environ["COCKPIT_TOKEN_SECRET_KEY"]),
}
for needle, value in replacements.items():
    template = template.replace(needle, value)
Path("/tmp/rag-health.alloy").write_text(template, encoding="utf-8")
PY

data-ingestion observability rag-health \
  --dsn-env RAG_HEALTH_POSTGRES_DSN \
  --env-label "$RAG_HEALTH_ENV_LABEL" \
  --host "${RAG_HEALTH_EXPORTER_HOST:-0.0.0.0}" \
  --port "$RAG_HEALTH_EXPORTER_PORT" \
  --poll-interval-seconds "$DB_HEALTH_POLL_INTERVAL_SECONDS" &

exporter_pid=$!

cleanup() {
  kill "$exporter_pid" 2>/dev/null || true
  wait "$exporter_pid" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

alloy run /tmp/rag-health.alloy
