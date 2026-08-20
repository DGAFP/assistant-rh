# RAG Quality Evaluation

Reusable goldset evaluation runs from `goldset_questions_v2` and writes:

- local JSON/CSV artifacts under `.cache/assistant-rh/evals/`;
- optional DB records in `rag_quality_eval_runs` and `rag_quality_eval_items`.

## Goldset

The current priority goldset is:

```bash
uv run --group dev python scripts/run_rag_quality_eval.py \
  --goldset-name iteration2_V1 \
  --tag iteration2 \
  --dsn-env SCW_POSTGRES_DSN_STAGING \
  --record-db \
  --init-schema
```

Use `--limit 1 --skip-ragas --skip-judge` for a cheap pipeline smoke test.

## Reproducibility

Evaluation runs use a base seed of `42` by default. Override it with `--seed`
or `RAG_EVAL_SEED`. The seed is stored in `eval_scope`, so runs with different
seeds are not de-duplicated or compared as equivalent.

For every question, the runner derives stable, independent seeds for the query
processor, context selector, answer generator, RAGAS, and judge. A three-vote
judge uses three distinct derived seeds: the votes remain independent while the
whole majority decision remains replayable.

The normal Streamlit inference path does not set a seed. Its behavior is
unchanged: the pipeline only sends the optional seed when an explicit caller,
such as this evaluation runner, supplies one. Provider or model updates can
still change outputs over long periods even when requests are seeded.

Existing unseeded baselines are intentionally not comparable with seeded runs.
When rolling this protocol out, record one seeded full run without a baseline
gate, review it, then configure that run as the new baseline before re-enabling
full adoption gates.

## Judge And RAGAS

The runner uses Scaleway through the OpenAI-compatible SDK for:

- the LLM-as-judge rubric;
- RAGAS LLM metrics (`faithfulness`, `context_precision`, `context_recall`).

Required variables:

```bash
SCALEWAY_API_KEY=...
SCALEWAY_BASE_URL=https://api.scaleway.ai/v1
SCALEWAY_JUDGE_MODEL=qwen3-235b-a22b-instruct-2507
```

`SCALEWAY_JUDGE_MODEL` is optional and defaults to `qwen3-235b-a22b-instruct-2507`
(an instruct model, not a reasoning one — reasoning judges are too slow/token-heavy
for the eval volume). It agreed with human PASS/BLOCKS reviewers on 90% of the
calibration set with zero false-PASS; `llama-3.1-70b-instruct` scored 81%.
RAGAS runs on a separate, faster model — `RAGAS_MODEL` (or `--ragas-model`),
default `llama-3.3-70b-instruct` — because its many statement/NLI sub-calls do not
need the higher-quality judge model. `RAGAS_MAX_TOKENS` is optional and defaults to
`16384`: on long French answers a smaller cap truncates the faithfulness
decomposition, and RAGAS then retries on every truncation and stalls the run.
RAGAS is still ~45s/question (sequential sub-calls), so a large run with RAGAS
enabled takes a while; deterministic retrieval metrics and the judge are the
primary signals.

The LLM-as-judge score is calibrated in code from four dimensions:

- `legal_correctness`;
- `completeness`;
- `gold_answer_alignment`;
- `source_support`.

The model still returns a raw overall score, but the stored `score` is capped
when the answer contradicts the gold answer, aligns poorly with it, is legally
weak, is incomplete, is weakly supported, or retrieval misses expected gold
sources. A total expected-source miss remains a hard cap; a partial expected-source
miss lowers the stored score but can still pass when answer quality stays above
threshold. The raw score is preserved as `raw_model_score` for auditability.

## Run De-Duplication

The run table stores a `config_fingerprint` computed from the same nested
`RAGConfig` used by the Streamlit chat runtime. De-duplication also stores an
`eval_scope` in run metadata so a small smoke run cannot suppress a later full
run. CI can avoid duplicate runs with:

```bash
uv run --group dev python scripts/run_rag_quality_eval.py \
  --goldset-name iteration2_V1 \
  --tag iteration2 \
  --dsn-env SCW_POSTGRES_DSN_STAGING \
  --record-db \
  --skip-if-started \
  --dedupe-scope config-and-git
```

A matching run is defined by:

- `goldset_name`;
- exact `tag_filter`;
- `config_fingerprint`;
- exact `eval_scope`, including selected question ids, `--limit`, `--seed`,
  judge/RAGAS enablement, judge model, and RAGAS model;
- current git SHA, unless `--dedupe-scope config` is used;
- status in `started`, `running`, or `completed`.

The GitHub workflow `.github/workflows/rag-quality-eval.yml` is tiered:

- Pushes already integrated into the protected `dev` and `staging` branches run
  a smoke eval on staging (`limit=5`, judge enabled, RAGAS skipped). Pull-request
  code is never executed with environment secrets.
- Manual `workflow_dispatch` runs in `full` mode run the full goldset and compare
  the selected trusted revision to a stored DB baseline from
  `rag_quality_eval_runs`.
- Manual production dispatches force full mode and require a comparable DB
  baseline.

Full baseline comparison requires a comparable baseline run: same DB target,
`goldset_name`, exact `tag_filter`, exact `eval_scope` (question ids, limit,
judge/RAGAS enablement and models), and `status='completed'`. The baseline can
be selected with `--baseline-run-id`, `--baseline-run-label`, or by the latest
completed comparable run. The candidate and baseline `config_fingerprint` values
are reported but not required to match, because some PRs intentionally change
runtime behavior.

The default full gate fails when:

- no comparable baseline is found;
- `judge_pass_rate` drops by more than `0.05`;
- `doc_recall_avg` drops by more than `0.05`;
- the candidate run itself fails.

## Outputs

The JSON artifact contains the full per-question diagnostics: answer, contexts,
pipeline metadata, deterministic retrieval metrics, RAGAS metrics, and judge
result.

The CSV artifact is the compact review surface with:

- question id and question;
- expected document sources;
- document recall, precision, hit rate, and MRR;
- judge score/pass/failure category;
- RAGAS metric columns.
