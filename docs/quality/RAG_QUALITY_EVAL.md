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

## Judge And RAGAS

The runner uses Scaleway through the OpenAI-compatible SDK for:

- the LLM-as-judge rubric;
- RAGAS LLM metrics (`faithfulness`, `context_precision`, `context_recall`).

Required variables:

```bash
SCALEWAY_API_KEY=...
SCALEWAY_BASE_URL=https://api.scaleway.ai/v1
SCALEWAY_JUDGE_MODEL=llama-3.1-70b-instruct
```

`SCALEWAY_JUDGE_MODEL` is optional and defaults to `llama-3.1-70b-instruct`.
`RAGAS_MAX_TOKENS` is optional and defaults to `4096` so faithfulness judgments
have enough budget for structured outputs.

The LLM-as-judge score is calibrated in code from four dimensions:

- `legal_correctness`;
- `completeness`;
- `gold_answer_alignment`;
- `source_support`.

The model still returns a raw overall score, but the stored `score` is capped
when the answer contradicts the gold answer, aligns poorly with it, is legally
weak, is incomplete, is weakly supported, or retrieval missed expected gold
sources. The raw score is preserved as `raw_model_score` for auditability.

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
- exact `eval_scope`, including selected question ids, `--limit`, judge/RAGAS
  enablement, and judge model;
- current git SHA, unless `--dedupe-scope config` is used;
- status in `started`, `running`, or `completed`.

The GitHub workflow `.github/workflows/rag-quality-eval.yml` runs the same CLI
for both the PR base SHA and head SHA when RAG quality files change. This keeps
the baseline and candidate comparable even when the runtime config is unchanged.

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
