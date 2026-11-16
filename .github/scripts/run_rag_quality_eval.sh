#!/usr/bin/env bash
# Assemble les arguments de scripts/run_rag_quality_eval.py à partir des
# variables résolues par resolve_rag_eval_params.sh, puis lance l'évaluation.
#
# Appelé par .github/workflows/rag-quality-eval.yml (étape « Run RAG quality
# eval »). Lit dans l'environnement : LIMIT, SKIP_RAGAS, SKIP_JUDGE,
# JUDGE_PROVIDER, JUDGE_VOTES, JUDGE_MODEL_OVERRIDE, COMPARE_BASELINE,
# BASELINE_RUN_ID, BASELINE_RUN_LABEL, ANY_GOLDSET, EVAL_MODE, GOLDSET_NAME,
# GOLDSET_TAG et RUN_LABEL.
set -euo pipefail
limit_args=()
if [[ "$LIMIT" != "0" && -n "$LIMIT" ]]; then
  limit_args+=(--limit "$LIMIT")
fi
ragas_args=()
judge_args=()
if [[ "$SKIP_RAGAS" == "true" ]]; then
  ragas_args+=(--skip-ragas)
fi
if [[ "$SKIP_JUDGE" == "true" ]]; then
  judge_args+=(--skip-judge)
else
  judge_args+=(--judge-provider "${JUDGE_PROVIDER:-openrouter}")
  judge_args+=(--judge-votes "${JUDGE_VOTES:-1}")
  if [[ -n "${JUDGE_MODEL_OVERRIDE:-}" ]]; then
    judge_args+=(--judge-model "$JUDGE_MODEL_OVERRIDE")
  fi
fi
baseline_args=()
if [[ "$COMPARE_BASELINE" == "true" ]]; then
  baseline_args+=(--require-baseline --gate-against-baseline)
  if [[ -n "${BASELINE_RUN_ID:-}" ]]; then
    baseline_args+=(--baseline-run-id "$BASELINE_RUN_ID")
  fi
  if [[ -n "${BASELINE_RUN_LABEL:-}" ]]; then
    baseline_args+=(--baseline-run-label "$BASELINE_RUN_LABEL")
  fi
fi
any_goldset_args=()
if [[ "${ANY_GOLDSET:-false}" == "true" ]]; then
  any_goldset_args+=(--any-goldset)
fi
dedupe_args=()
if [[ "$EVAL_MODE" == "smoke" ]]; then
  dedupe_args+=(--skip-if-started)
fi

uv run --no-sync python scripts/run_rag_quality_eval.py \
  --goldset-name "$GOLDSET_NAME" \
  --tag "$GOLDSET_TAG" \
  "${any_goldset_args[@]}" \
  "${limit_args[@]}" \
  "${ragas_args[@]}" \
  "${judge_args[@]}" \
  "${baseline_args[@]}" \
  --record-db \
  --init-schema \
  "${dedupe_args[@]}" \
  --dedupe-scope config-and-git \
  --run-label "$RUN_LABEL" \
  --output-dir ".cache/assistant-rh/evals/$RUN_LABEL" \
  | tee "$RUNNER_TEMP/rag-quality.json"
