# Data quality gates

Issue #36 adds a post-ingestion database quality gate for Scaleway staging and production.

The gate is versioned in:

- `config/data_quality_gates.json`

The structural checker is exposed through the canonical ingestion CLI. The DSN is
resolved from the environment by default (`SCW_POSTGRES_DSN` / `APP_POSTGRES_DSN` /
`STREAMLIT_POSTGRES_DSN`); pass `--dsn` or `--dsn-env` only to override it:

```bash
uv run data-ingestion quality gates \
  --target-env staging \
  --source service_public \
  --source legifrance \
  --blocking \
  --json-output artifacts/data-quality/report.json \
  --markdown-output artifacts/data-quality/summary.md
```

## Checks

The v1 structural checks are intentionally DB-local and source-scoped:

- required critical tables exist
- selected-source row counts meet configured minimums
- configured expected IDs are present
- key text columns are not blank
- `max(updated_at)` is fresh where the table has a configured freshness column

The expected source IDs come from the existing source manifests:

- Service-Public: `config/service_public_fiches.json`
- Legifrance: `config/legifrance_article_cids.json`

### Embedding coverage

Embedding coverage is **not** part of this command. It is gated by the read-only
audit shipped with the embedding backfill (issue #133), so a single
implementation owns embedding-coverage logic (issue #172):

```bash
uv run data-ingestion embeddings legifrance --check-only --coverage-min-pct 100
```

`--check-only` opens an autocommit connection, runs aggregate `SELECT`s, prints a
coverage report as JSON, and exits non-zero when coverage is below the threshold —
without loading models, calling APIs, or writing to the database.

## Workflow behavior

All staging and production data-engineering workflows run the structural gate and
publish JSON and Markdown reports. When an embeddings backfill was selected, a
separate step runs `data-ingestion embeddings <source> --check-only` per source.

Blocking is scoped to DB-writing runs:

- ingestion jobs block on the structural gate (tables, coverage, text, freshness)
- embeddings jobs block on the separate embedding-coverage step
- medallion-only / promotion runs publish reports without failing on the structural gate

Reports (structural Markdown/JSON plus per-source embedding JSON) are uploaded as
GitHub Actions artifacts and the structural summary is appended to the job summary.
