# RAG Evaluation Process

This document defines the evaluation process to follow when changing the RAG
system, runtime configuration, prompts, goldsets, or source corpora.

Use this process to answer two different questions:

- did the system still retrieve the right evidence?
- did the final answer remain legally correct, complete, and source-supported?

The reusable evaluation runner is documented in
[`RAG_QUALITY_EVAL.md`](./RAG_QUALITY_EVAL.md).

## Principles

RAG quality must be evaluated in layers. A single aggregate score is not enough,
because retrieval, context construction, source data, and generation can fail
independently.

Evaluate these layers separately:

1. Data and source health.
2. Goldset compatibility.
3. Retrieval quality.
4. Context selection and assembly.
5. Final answer quality.
6. Human review for high-risk examples.

Do not compare two runs unless they use the same environment, question ids,
goldset filters, runtime config, judge settings, and source snapshot.

## Change Classes

Classify each change before choosing the evaluation depth.

| Change class | Examples | Minimum check |
| --- | --- | --- |
| UI-only | Display, admin page copy, non-RAG controls | Unit/lint checks only |
| Prompt or generation | system prompt, generator model, temperature | Smoke eval, then scoped answer eval |
| Retrieval config | tables, top-k, hybrid alpha, reranker, embeddings | Retrieval and answer eval |
| Selector/context | selector prompt/model, token budget, context mode, legal refs | Context and answer eval |
| Source ingestion | Service-Public, MATTE, DGAFP, RGRH, embeddings, schema | Data preflight, goldset validation, full eval |
| Goldset change | source ids, tags, question set, expected answers | Goldset validation and baseline refresh |
| Workflow/tooling | CI eval workflow, runner, eval schema | Tool tests and one staging smoke run |

## Required Preflight

Run preflight before any expensive evaluation. If preflight fails, evaluation
results are not trustworthy.

Check:

- target DSN and environment are explicit;
- required corpus tables exist;
- corpus row counts are plausible;
- embedding coverage is non-zero for retrieval tables;
- runtime `rag_config` is readable;
- prompt names referenced by config exist;
- goldset rows exist for the selected `goldset_name` and tags;
- `gold_sources` parse correctly as single-source and multi-source rows;
- expected source ids still map to current corpus identifiers.

For source changes, also check the source-family distribution:

- MATTE;
- Service-Public;
- DGAFP;
- RGRH;
- mixed-source rows.

## Goldset Rules

Goldsets are contract tests for behavior. Treat them as data with schema and
semantics, not as loose CSV fixtures.

Rules:

- `gold_sources` may contain multiple expected sources.
- Multi-source rows must be preserved as arrays, not split by commas inside
  legal citations.
- Source ids must be matched against current corpus identifiers, not obsolete
  table names.
- Goldset filters must be JSON-array-aware when `gold_sources` is stored as a
  JSON list.
- Every run must record the exact question ids evaluated.
- A smoke run must not de-duplicate or suppress a later full run.

When adapting old goldsets, back up the existing rows first and run a strict
readback parse after mutation.

## Evaluation Levels

### 1. Smoke Evaluation

Use this for fast PR feedback and runner sanity.

Typical scope:

- 3 to 10 questions;
- same goldset and tags as the full run;
- judge enabled unless the change is only runner plumbing;
- DB recording enabled on staging when available.

The smoke run answers: does the pipeline execute end to end on representative
questions?

### 2. Scoped Evaluation

Use this for normal RAG changes.

Typical scope:

- representative subset by source family;
- deterministic retrieval metrics;
- final answer judge;
- per-question artifact review for failures and regressions.

The scoped run answers: did the change improve or preserve behavior on the
targeted surface?

### 3. Full Evaluation

Use this before merging high-risk RAG or source changes.

Required for:

- ingestion changes;
- embeddings changes;
- source table schema changes;
- selector/context changes;
- generator prompt changes with legal-behavior impact;
- goldset migrations.

The full run answers: is the change safe across the full current goldset?

## Baseline Versus Candidate

After a change is integrated into `dev` or `staging`, the staging smoke eval is
run from that protected branch. It is intentionally small and should answer
only: did this change obviously break the RAG runner or quality path? PR head
code is not executed with staging or production secrets.

Run a full baseline-versus-candidate comparison only when:

- the workflow is manually dispatched in `full` mode;
- a production/release gate is being evaluated.

The stored baseline and candidate must use:

- same DSN;
- same `goldset_name`;
- same tag filters;
- same question ids;
- same judge model and RAGAS settings;
- same source snapshot.

The run metadata must include:

- git SHA;
- config fingerprint;
- goldset name;
- tag filters;
- question ids;
- limit;
- judge and RAGAS enablement;
- judge model;
- output artifact paths.

The candidate and baseline `config_fingerprint` values should be reported. They
do not need to match when the PR intentionally changes runtime behavior.

## Metrics To Review

Review deterministic metrics first:

- hit rate;
- document recall;
- document precision;
- MRR;
- missing expected sources;
- retrieved source-family distribution.

Then review answer metrics:

- judge score;
- pass rate;
- legal correctness;
- completeness;
- gold-answer alignment;
- source support;
- material contradictions;
- RAGAS faithfulness and context metrics when enabled.

Then review the retrieval funnel (`deterministic_metrics.stages` per item,
`aggregate.stage_metrics` per run). For each retrieval attempt it reports
hit rate and document recall at four upstream stages:

- `pool`: chunks before section rerank (was the gold retrieved at all?);
- `sections_top20` / `sections_top12`: aggregated sections in served order
  (did the rerank/candidate cut drop it?);
- `selector_kept`: sections actually served (did the selector drop it?).

Compare stages per corpus (SQL `GROUP BY q.source`) to attribute a recall loss
to retrieval, aggregation, the candidate cut, or the selector without
replaying the pipeline. These stages are diagnostics, not gates: the served
context remains measured by the top-level deterministic metrics.

Always inspect examples behind regressions. Aggregate averages are not enough.

## Regression Triage

When a run regresses, classify the failure before changing code.

Common categories:

- source data missing or stale;
- embeddings missing;
- gold source id no longer maps to the corpus;
- retrieval missed the expected document;
- aggregation/reranking demoted the right evidence;
- selector removed useful context;
- token budget trimmed required context;
- legal references were not injected;
- generator ignored or contradicted context;
- judge failed or was misconfigured.

Fix the layer that failed. Do not tune prompts to hide missing data or broken
retrieval.

## Merge Gates

A RAG change is merge-ready when:

- tests and lint pass;
- preflight passes;
- smoke evaluation passes;
- baseline and candidate are comparable, or the exception is documented;
- no critical source family regresses;
- any metric drop has inspected examples and an accepted explanation;
- multi-source goldset rows are verified;
- eval artifacts are recorded and linked in the PR.

For source or ingestion changes, also require:

- row counts and embedding coverage are recorded before and after;
- old source identifiers still resolve, or goldsets are migrated;
- destructive operations have a rollback or backup path;
- one staging replay has completed successfully.

## PR Checklist

Include this information in RAG-impacting PRs:

- change class;
- target environment and DSN variable used;
- preflight result;
- smoke eval command and result;
- full or scoped eval command and result, when required;
- baseline run id and candidate run id, when recorded;
- notable regressions and inspected examples;
- source-family impact;
- tests run.

## Operational Notes

- Prefer staging for durable eval records.
- Never run destructive ingestion or migrations without confirming the target
  DSN.
- Keep local smoke artifacts under `.cache/assistant-rh/evals/`.
- Use DB-recorded evals for comparisons that will inform merge or promotion.
- Keep the evaluation runner deterministic where possible; judge and RAGAS are
  secondary to deterministic retrieval diagnostics.
- If a goldset or source table is adapted, update this process and the runner
  docs in the same PR.
