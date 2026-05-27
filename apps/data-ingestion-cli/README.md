# Data Ingestion CLI

`apps/data-ingestion-cli` is the canonical application entrypoint for Assistant RH data ingestion jobs.

The executable command is:

```bash
uv run data-ingestion <domain> <job> [job args]
```

`assistant-rh-data` remains available as a backward-compatible alias during the migration.

## Commands

```bash
uv run data-ingestion service-public medallion --help
uv run data-ingestion service-public ingest --help

uv run data-ingestion legifrance bulk-dump --help
uv run data-ingestion legifrance medallion --help
uv run data-ingestion legifrance ingest --help

uv run data-ingestion embeddings backfill --help
uv run data-ingestion embeddings service-public --help
uv run data-ingestion embeddings legifrance --help
```

## Boundaries

This app owns the human-facing CLI command. The job implementations stay in `packages/data-engineering` so they remain reusable by Docker images, CI, and future applications.

Root-level scripts in `scripts/` may still exist for compatibility or unrelated operational tooling, but new data ingestion documentation should prefer `data-ingestion`.
