# RAG trace observability

This runbook describes the Assistant RH per-turn RAG trace pipeline:

- Streamlit writes compact stage events to `rag_trace_events`.
- Streamlit optionally exports the same events as OTLP/HTTP spans.
- Grafana reads trace drilldowns from a Tempo-compatible data source and aggregate trace metrics from the existing RAG Health Prometheus exporter.

## Cockpit and Grafana prerequisites

Create or identify these in the target Scaleway Cockpit/Grafana workspace:

- a traces/Tempo-compatible OTLP HTTP ingest endpoint
- a token or header value accepted by that OTLP ingest endpoint
- a Grafana API token allowed to import dashboards
- a Grafana Tempo data source connected to the same trace ingest backend
- the RAG Health exporter deployed for the target environment, so `assistant_rh_rag_trace_*` metrics are pushed to Cockpit metrics

## Streamlit trace export configuration

Set these values on the GitHub environment used by the Streamlit deploy workflow (`scaleway-staging` or `scaleway-production`):

```bash
RAG_TRACING_ENABLED=true
OTEL_SERVICE_NAME=assistant-rh
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://<trace-datasource-id>.traces.cockpit.fr-par.scw.cloud/otlp/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ...
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is also supported. When only the base endpoint is set, the application appends `/v1/traces` for standard
OTLP endpoints and `/otlp/v1/traces` for Scaleway Cockpit trace endpoints.

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
- `metrics_datasource`: the Cockpit Prometheus-compatible source used by RAG Health
- `env`: `staging`, `prod`, or `All`
- `trace_id_filter`: keep `.*` for recent traces, or enter a trace ID regex to narrow the table
- `trace_id`: selected trace shown by the waterfall; leave the default until clicking a table row or paste an exact trace ID

## Verification

1. Ask a RAG question in the deployed Streamlit app.
2. Confirm the turn appears in `chat_runs` with a `trace_id`.
3. Confirm `rag_trace_events` has rows for the same `turn_id` and `trace_id`.
4. Confirm the RAG Health exporter exposes `assistant_rh_rag_trace_*` metrics on `/metrics`.
5. Open the Grafana dashboard and use the `Recent RAG traces` table to find the turn or trace.
6. Click the trace ID to populate the dashboard selected `trace_id` variable.
7. Confirm the selected trace waterfall shows the pipeline spans and the Prometheus panels show trace volume, latency, error, and freshness metrics.

Trace export is best effort. PostgreSQL persistence remains the primary admin debugging source if the external trace backend is unavailable, but the Grafana dashboard intentionally avoids a direct PostgreSQL data source requirement.
