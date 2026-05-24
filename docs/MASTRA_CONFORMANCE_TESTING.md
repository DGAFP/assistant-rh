# Mastra Port Conformance Testing

This document defines the execution model for validating Mastra parity against the current Python v3 pipeline.

## Principles

1. **Compatibility first**: migration is accepted only if user-visible behavior stays aligned.
2. **Step visibility before end-to-end claims**: compare retrieval/selection/context internals, not just final answers.
3. **Automatable evidence**: every run produces a JSON report artifact.

## Runner

Use:

```bash
uv run python scripts/run_mastra_conformance.py --help
```

The runner supports:

- fixture-based query execution (`--queries-file`)
- DB goldset execution (`--goldset-name`)
- Python-only baseline snapshots
- Python vs Mastra comparison against `/v1/chat/completions`
- optional threshold enforcement (`--enforce-thresholds`)

## Report structure

Main report keys:

- `results[]`: per-query Python run, candidate run, and comparison metrics
- `summary`: aggregate metrics across queries
- `threshold_failures`: list of failing metrics (when candidate mode is active)

## Initial acceptance thresholds

- `intent_match_rate >= 0.95`
- `retrieval_overlap_topk_avg >= 0.80`
- `section_overlap_topk_avg >= 0.80`
- `context_overlap_topk_avg >= 0.75`
- `answer_token_jaccard_avg >= 0.70`
- `latency_ratio_p95 <= 1.30`

Thresholds can be overridden with `--thresholds-file`.

## Candidate metadata contract (recommended)

For full step-level parity metrics, candidate responses should expose metadata keys matching Python pipeline output:

- `intent`
- `theme`
- `needs_legal_search`
- `retrieved_chunks` (with `chunk_id`)
- `aggregated_sections` (with `section_id`)
- `context_items_ref` (with `section_id`)

This can be returned in either:

- top-level response `metadata`, or
- `choices[0].message.metadata`

## CI strategy

Two tiers:

1. **PR smoke**: small query subset (e.g. 15–25 queries)
2. **Nightly full parity**: larger goldset sample (e.g. 100+ queries)

Fail PRs only on P0 regressions; keep broader diagnostics as artifacts.
