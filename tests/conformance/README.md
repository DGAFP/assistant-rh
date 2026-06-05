# Conformance tests (Python v3 ↔ Mastra)

This folder contains fixtures and output reports for parity checks between:

- **Baseline:** current Python `assistant_rh_rag_pipeline`
- **Candidate:** Mastra OpenAI-compatible endpoint

## Query fixture format

`queries.sample.jsonl` uses one JSON object per line:

```json
{"id":"q1","query":"...","conversation_history":[{"role":"user","content":"..."}],"tags":["optional"]}
```

Required fields:

- `id`
- `query`

Optional fields:

- `conversation_history`
- `tags`

## Run

### 0) Dump per-stage Python baselines (replay artifacts)

```bash
uv run python scripts/dump_stage_baselines.py \
  --queries-file tests/conformance/queries.sample.jsonl \
  --output-dir tests/conformance/baselines/queries-sample
```

This generates per-query stage files (`00_input.json` → `06_generator.json`)
plus a `manifest.json` including git SHA, config snapshot, and prompt hashes.

### 0.5) Run Mastra stage conformance in replay mode (no live Python)

From workspace root:

```bash
pnpm --filter @assistant-rh/mastra-pipeline query-processor:conformance
pnpm --filter @assistant-rh/mastra-pipeline retriever:conformance
pnpm --filter @assistant-rh/mastra-pipeline section-aggregator:conformance
pnpm --filter @assistant-rh/mastra-pipeline context-selector:conformance
pnpm --filter @assistant-rh/mastra-pipeline context-builder:conformance
pnpm --filter @assistant-rh/mastra-pipeline rag-pipeline:conformance
```

These default to:

- `--mode replay`
- `--baseline-dir tests/conformance/baselines/queries-sample`

Use `--mode live` to force an on-the-fly Python baseline run for manual checks.

## CI conformance matrix (PR-C + PR-D)

Workflow: `.github/workflows/conformance.yml`

- **Required deterministic gates** (threshold-enforced):
  - `query-processor`
  - `retriever`
  - `section-aggregator`
  - `context-selector`
  - `context-builder`
- **Informational only** (non-blocking):
  - `rag-pipeline` (E2E)

Thresholds for required replay-stage gates are sourced from
`tests/conformance/thresholds.replay.json`.

### Query-processor LLM replay cache

Replay mode for query-processor uses a committed cache file:

- `tests/conformance/replay-cache/query-processor.intent.v1.json`

Useful commands:

```bash
# Strict replay (default for query-processor conformance)
pnpm --filter @assistant-rh/mastra-pipeline query-processor:conformance

# Refresh cache from live model calls
pnpm --filter @assistant-rh/mastra-pipeline query-processor:conformance:record-cache

# Rebuild cache directly from baseline stage artifacts
uv run python scripts/build_query_processor_replay_cache.py \
  --baseline-dir tests/conformance/baselines/queries-sample \
  --output tests/conformance/replay-cache/query-processor.intent.v1.json
```

In CI, replay mode is expected to be cache-hit complete for deterministic gating.

### Manual baseline refresh workflow (PR-E)

Workflow: `.github/workflows/conformance-refresh-baselines.yml`

Use this workflow when you want to refresh baseline artifacts and (optionally)
open/update a PR automatically.

Key inputs:

- `source_mode`: `sample` or `goldset`
- `baseline_dir`: target baseline path
- `refresh_query_processor_cache`: rebuild replay cache from new baselines
- `create_pull_request`: open/update PR with resulting diffs

This workflow requires staging DB + model credentials because baseline generation
executes the Python pipeline.

### Nightly extended run (PR-E)

Workflow: `.github/workflows/conformance-nightly.yml`

- Runs nightly (`cron`) and on manual dispatch.
- Runs an explicit precheck for `goldset_questions_v2` prerequisites (table/columns/data).
- Builds goldset-backed baselines in a temporary workspace.
- Rebuilds query-processor replay cache for that nightly query set.
- Runs replay-mode stage conformance and uploads artifacts.
- Publishes a markdown summary in the GitHub Actions run summary.

Nightly is informational monitoring and does **not** gate merges.

### Goldset bootstrap seed flow (issues #181-#183)

When staging is missing goldset prerequisites, bootstrap with a reproducible seed:

```bash
# 1) Export deterministic seed from local Supabase
uv run python scripts/export_goldset_seed.py \
  --goldset-name synthetic_docs_v1 \
  --limit 100 \
  --output tests/conformance/seeds/goldset_questions_v2.synthetic_docs_v1.jsonl

# 2) Load seed into staging (after applying Supabase migrations)
uv run python scripts/load_goldset_seed.py \
  --input tests/conformance/seeds/goldset_questions_v2.synthetic_docs_v1.jsonl \
  --target-dsn-env SCW_POSTGRES_DSN
```

Use `--replace-goldset` on `load_goldset_seed.py` if you want a full replace for
seeded goldset rows before upsert.

### Scaleway migration workflow (Supabase CLI)

Workflow: `.github/workflows/db-migrations-scaleway.yml`

- Uses `supabase db push --db-url ...` to apply `supabase/migrations/*.sql` to Scaleway staging or production.
- Staging runs automatically on `main` pushes when migration/seed files change.
- Staging seed load is path-based: `scripts/load_goldset_seed.py` runs only when a changed JSONL seed matching `tests/conformance/**/goldset_questions_v2*.jsonl` is detected on push.
- Manual dispatch supports `force_seed=true` to run seed load even without JSONL changes.
- Production run is manual-only and migration-only (seed load disabled by design).

### Sticky PR conformance summary comment (PR-E)

`conformance.yml` now posts/updates one sticky PR comment (marker:
`<!-- conformance-summary -->`) with stage status and key metrics, instead of
creating a new comment on each run.

(`tests/conformance/thresholds.json` remains the default threshold set for
`scripts/run_mastra_conformance.py` Python-vs-candidate comparisons.)

### Planned follow-ups (post PR-C)

- Promote retriever gate from "infra-tolerant" to strict once dual-index tables
  (`rag_chunks_albert`, `rag_chunks_scaleway`) are present in the CI target DB.
- Extract conformance-runner stage config (selector replay overrides, threshold
  mappings, and stage metadata) into a shared config surface to reduce duplicated
  per-runner wiring.

### 1) Python baseline only

```bash
uv run python scripts/run_mastra_conformance.py \
  --queries-file tests/conformance/queries.sample.jsonl \
  --output tests/conformance/reports/python_baseline.json
```

### 2) Python vs Mastra endpoint

```bash
uv run python scripts/run_mastra_conformance.py \
  --queries-file tests/conformance/queries.sample.jsonl \
  --candidate-base-url http://localhost:4111 \
  --candidate-model assistant-rh \
  --output tests/conformance/reports/python_vs_mastra.json
```

### 3) DB-driven goldset execution

```bash
uv run python scripts/run_mastra_conformance.py \
  --goldset-name beta_evaluated \
  --limit 50 \
  --candidate-base-url http://localhost:4111 \
  --output tests/conformance/reports/beta50_python_vs_mastra.json
```

## What is compared

If candidate metadata includes stage refs aligned with Python metadata keys,
the report computes:

- `intent_match_rate`
- `theme_match_rate`
- `needs_legal_search_match_rate`
- `retrieval_overlap_topk_avg`
- `section_overlap_topk_avg`
- `context_overlap_topk_avg`

Always computed when candidate answer is available:

- `answer_token_jaccard_avg`
- `answer_length_ratio_avg`
- `latency_ratio_avg`, `latency_ratio_p95`

## Notes

- Threshold checking is optional (`--enforce-thresholds`).
- Threshold defaults can be versioned in `tests/conformance/thresholds.json`.
- Retriever ordering is stabilized with deterministic tie-breaks (score desc, then stable ids) so baseline snapshots do not drift from thread-completion order.
- Cross-source RRF scores are rescaled to a stable `[0,1]` range before downstream stages to avoid sensitivity to raw fused-score magnitude.
- Reports are JSON artifacts suitable for CI and manual diffing.
- This harness is intentionally compatibility-focused, not model-quality-optimization-focused.

## Stage contracts

JSON Schemas for stage-level replay contracts live in:

- `tests/conformance/contracts/query-processor.schema.json`
- `tests/conformance/contracts/retriever.schema.json`
- `tests/conformance/contracts/section-aggregator.schema.json`
- `tests/conformance/contracts/context-selector.schema.json`
- `tests/conformance/contracts/context-builder.schema.json`
- `tests/conformance/contracts/generator.schema.json`
