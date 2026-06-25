# RAG trace observability

This runbook describes the Assistant RH per-turn RAG trace pipeline:

- Streamlit writes compact stage events to `rag_trace_events`.
- Streamlit optionally exports the same events as OTLP/HTTP spans.
- Grafana reads traces from a Tempo-compatible data source and joins drilldown tables from PostgreSQL.

## Cockpit and Grafana prerequisites

Create or identify these in the target Scaleway Cockpit/Grafana workspace:

- a traces/Tempo-compatible OTLP HTTP ingest endpoint
- a token or header value accepted by that OTLP ingest endpoint
- a Grafana API token allowed to import dashboards
- a Grafana PostgreSQL data source that can read `rag_trace_events`
- a Grafana Tempo data source connected to the same trace ingest backend

## Streamlit trace export configuration

Set these values on the GitHub environment used by the Streamlit deploy workflow (`scaleway-staging` or `scaleway-production`):

```bash
RAG_TRACING_ENABLED=true
OTEL_SERVICE_NAME=assistant-rh
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://.../v1/traces
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ...
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is also supported. When only the base endpoint is set, the application appends `/v1/traces`.

Keep `OTEL_EXPORTER_OTLP_HEADERS` as a GitHub secret. The Streamlit deploy helper passes it to the Scaleway container as a secret environment variable.

Deploy Streamlit after setting the values:

- staging: `Streamlit Deploy Staging`
- production: release deployment or `Streamlit Deploy Production` rollback/redeploy flow

If `RAG_TRACING_ENABLED=true` but neither OTLP endpoint is set, deployment fails instead of silently starting without trace export.

## Dashboard import

Set these values on the same GitHub environment:

```bash
COCKPIT_GRAFANA_URL=https://...
COCKPIT_GRAFANA_API_TOKEN=...
RAG_TRACE_GRAFANA_FOLDER_UID=... # optional
```

Run the manual `RAG Trace Dashboard Deploy` workflow and select the target environment. The workflow imports
`config/grafana/rag-trace-explorer-dashboard.json` with the stable dashboard UID `assistant-rh-rag-trace-explorer`.

After import, select the Grafana variables:

- `trace_datasource`: the Tempo-compatible trace source
- `postgres_datasource`: the PostgreSQL source connected to the Assistant RH database
- `env`: `staging`, `prod`, or `All`

## Verification

1. Ask a RAG question in the deployed Streamlit app.
2. Confirm the turn appears in `chat_runs` with a `trace_id`.
3. Confirm `rag_trace_events` has rows for the same `turn_id` and `trace_id`.
4. Open the Grafana dashboard and filter by that `turn_id` or `trace_id`.
5. Confirm the Tempo panels show spans and the PostgreSQL panels show bounded stage/chunk drilldowns.

Trace export is best effort. PostgreSQL persistence remains the primary debugging source if the external trace backend is unavailable.
