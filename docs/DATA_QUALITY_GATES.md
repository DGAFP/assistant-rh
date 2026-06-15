# Data quality gates

Issue #36 adds a post-ingestion database quality gate for Scaleway staging and production.

The gate is versioned in:

- `config/data_quality_gates.json`

The checker is exposed through the canonical ingestion CLI:

```bash
uv run data-ingestion quality gates \
  --target-env staging \
  --source service_public \
  --source legifrance \
  --dsn-env SCW_POSTGRES_DSN \
  --blocking \
  --json-output artifacts/data-quality/report.json \
  --markdown-output artifacts/data-quality/summary.md
```

## Checks

The v1 checks are intentionally DB-local and source-scoped:

- required critical tables exist
- selected-source row counts meet configured minimums
- configured expected IDs are present
- key text columns are not blank
- `max(updated_at)` is fresh where the table has a configured freshness column
- embedding columns meet coverage thresholds when an embeddings backfill was selected

The expected source IDs come from the existing source manifests:

- Service-Public: `config/service_public_fiches.json`
- Legifrance: `config/legifrance_article_cids.json`

## Workflow behavior

All staging and production data-engineering workflows publish JSON and Markdown reports.

The gate is blocking only after a workflow writes to Postgres:

- ingestion jobs block on selected-source table, coverage, text, and freshness checks
- embeddings jobs block on selected embedding coverage checks
- medallion-only jobs publish a report but do not fail the workflow

Reports are uploaded as GitHub Actions artifacts and appended to the job summary.
