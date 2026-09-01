# M0 deterministic parity replays

The M0b bundle freezes the legacy Python runtime immediately before the
hexagonal API extraction. It is a behavioral contract, not a quality-tuning
dataset. Its inputs are synthetic/public HR questions and contain no personal
data.

## Versioned reference

- Queries: `tests/conformance/queries.m0-api-parity.jsonl`
- Baseline: `tests/conformance/baselines/m0-api-parity-dev-9bf1cf0/`
- Source revision: `9bf1cf0cb92420e9e551f811edadb1d7129244b1`
- Related live reference: M0a run `m0a_api_parity_dev_20260901_rerun1`
  (run #240; the failed partial attempt #238 is recorded in the experiment
  journal)
- Live snapshot: `docs/evals/evidence/m0a_api_parity_dev_20260901.json`
- Runtime config fingerprint: `51d6256bace3d6c3c36b26ea0dee66b79ecc214f78e4b67dc6b76525e1bbf1ce`
- Replay fingerprint: `f5e9ffefe588248a352d7ac18a556df7bff6270a2879e7ddd32acc128733e02b`

The bundle contains `00_input.json`, one exact JSON input/output contract for
each pipeline stage (`01` through `06`), and `07_pipeline_result.json` for each
fixture. Runtime-only timing, turn IDs, trace IDs, and timestamped trace events
are deliberately excluded from the structured result. Configuration, active
prompt hashes, models/providers, artifact hashes, observed branch coverage, and
the overall replay fingerprint are stored in `manifest.json`.

## Record on the frozen live runtime

This command requires the staging DB and provider credentials. It refuses to
record from another Git revision and fails when an observed branch differs from
the fixture's declared expectation.

```bash
uv run python scripts/dump_stage_baselines.py \
  --queries-file tests/conformance/queries.m0-api-parity.jsonl \
  --output-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0 \
  --expected-git-sha 9bf1cf0cb92420e9e551f811edadb1d7129244b1 \
  --reference-run-id 240 \
  --source-environment scaleway-staging
```

Refreshing the reference is an explicit milestone decision. Do not overwrite
it to make a later implementation pass.

## Verify or compare offline

Self-check the committed bundle with no DB, model, or network dependency:

```bash
uv run python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0
```

After a candidate runtime exports the same file layout, compare every stage and
the structured result using exact JSON equality:

```bash
uv run python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0 \
  --actual-dir /path/to/candidate-export
```

The candidate conformance runner must replay the recorded provider/search
outputs at its ports rather than call live models or live retrieval. Exact
equality is the extraction contract; live quality remains the separate M0a
gate.

The verifier also checks JSON Schemas, artifact SHA-256 hashes, bundle
fingerprint integrity, fixture completeness, declared branch coverage, safe
relative paths, and the input personal-data guard.

## Branch matrix

| Fixture | Contract branch |
|---|---|
| `rag-acronym-contract` | normal RAG path, acronym expansion, MATTE request scope |
| `rag-legal-dgafp` | legal-search gate and DGAFP retrieval |
| `rag-conversation-followup` | conversation-history/follow-up processing |
| `rag-ministry-mso` | request-scoped MSO + shared-source retrieval |
| `short-circuit-chit-chat` | direct-response chit-chat short circuit |
| `short-circuit-document-request` | document-request refusal short circuit |
| `short-circuit-out-of-scope` | out-of-scope refusal short circuit |

The manifest is authoritative for observed values. A selector rejection/retry
fixture may be added only when the frozen runtime produces that branch reliably
without changing pipeline settings.
