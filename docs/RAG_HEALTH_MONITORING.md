# RAG data health monitoring

This runbook describes the Assistant RH RAG data-health exporter and the Scaleway Cockpit/Grafana dashboard.

For per-turn pipeline traces and the Tempo/PostgreSQL dashboard, see
[`RAG_TRACE_OBSERVABILITY.md`](RAG_TRACE_OBSERVABILITY.md).

## Architecture

The monitoring container runs two processes:

- `data-ingestion observability rag-health`, a read-only Python exporter on `/metrics` and `/healthz`
- Grafana Alloy, scraping the local exporter and remote-writing metrics to Scaleway Cockpit

The exporter polls PostgreSQL every `DB_HEALTH_POLL_INTERVAL_SECONDS` seconds. It reports document, section, chunk, embedding, freshness, integrity, and aggregate RAG trace metrics with low-cardinality labels.

## Cockpit prerequisites

Create these in Scaleway Cockpit before deployment:

- a custom metrics data source in the same region as the container
- a Cockpit token with Push permission for metrics in that region

Set the deployment environment values:

```bash
RAG_HEALTH_POSTGRES_DSN=postgresql://...
COCKPIT_METRICS_PUSH_URL=https://...metrics.cockpit.fr-par.scw.cloud/api/v1/push
COCKPIT_TOKEN_SECRET_KEY=...
DB_HEALTH_POLL_INTERVAL_SECONDS=300
```

`RAG_HEALTH_POSTGRES_DSN` may be the same value as `SCW_POSTGRES_DSN`, but it is intentionally named separately so the monitoring container can use a read-only database user later.

## Deploy

Use the manual `RAG Health Deploy` GitHub workflow and choose either:

- `scaleway-staging`
- `scaleway-production`

The workflow builds `Dockerfile.rag_health_exporter`, pushes `rag-health-exporter`, and upserts a private Scaleway Serverless Container with `min-scale=1`.

## Dashboard and alerts

Import `config/grafana/rag-health-dashboard.json` into the Cockpit Grafana instance and select the Cockpit Prometheus-compatible
data source.

The dashboard environment selector is explicit: `staging`, `prod`, or `All`. If `prod` panels are empty, run the `RAG Health
Deploy` workflow for `scaleway-production` and confirm that the production environment has the same Cockpit values plus a
production `RAG_HEALTH_POSTGRES_DSN` or `SCW_POSTGRES_DSN`.

Alert rule examples are in `config/grafana/rag-health-alerts.yaml`:

- DB polling failure for more than 10 minutes
- present chunk table with zero rows
- embedding coverage below 99%
- orphan or missing-reference integrity issues

The same exporter also emits `assistant_rh_rag_trace_*` metrics from `rag_trace_events` so the RAG trace dashboard can use Cockpit Tempo plus Cockpit metrics without requiring a direct Grafana PostgreSQL data source.

## Local checks

Run a one-shot collection against your configured local DB:

```bash
uv run data-ingestion observability rag-health --env-label staging --once
```

For a production DSN, use the prod label:

```bash
RAG_HEALTH_POSTGRES_DSN="$SCW_POSTGRES_DSN" \
uv run data-ingestion observability rag-health --env-label prod --once
```

Run the HTTP exporter locally:

```bash
RAG_HEALTH_POSTGRES_DSN="$SCW_POSTGRES_DSN" \
uv run data-ingestion observability rag-health --env-label staging --port 9108
```

Then open `http://127.0.0.1:9108/metrics`.
