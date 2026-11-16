#!/usr/bin/env bash
# Résout et valide les paramètres d'évaluation RAG, puis écrit les variables
# retenues dans $GITHUB_ENV.
#
# Appelé par .github/workflows/rag-quality-eval.yml (étape « Resolve eval
# parameters »). Les entrées de workflow_dispatch et les valeurs par défaut
# pour les déclenchements automatiques sont validées ici plutôt que dans le
# YAML : le script est testable, relu en diff, et ne dépend pas de
# l'indentation du workflow.
#
# Variables d'entrée attendues dans l'environnement (voir le bloc env de
# l'étape) : EVENT_NAME, EVAL_MODE_INPUT, TARGET_ENVIRONMENT_INPUT,
# GOLDSET_NAME_INPUT, GOLDSET_TAG_INPUT, LIMIT_INPUT, SKIP_RAGAS_INPUT,
# SKIP_JUDGE_INPUT, ANY_GOLDSET_INPUT, BASELINE_RUN_ID_INPUT,
# BASELINE_RUN_LABEL_INPUT, RUN_LABEL_SUFFIX_INPUT, GITHUB_SHA_INPUT,
# GITHUB_REF_NAME_INPUT, ainsi que DEFAULT_BASELINE_RUN_ID et
# DEFAULT_BASELINE_RUN_LABEL définis au niveau du job.
set -euo pipefail
validate_required_name() {
  local name="$1"
  local value="$2"
  if (( ${#value} > 128 )) || [[ ! "$value" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    echo "$name invalide (1-128 caractères parmi [A-Za-z0-9._:-])." >&2
    exit 1
  fi
}
validate_optional_label() {
  local name="$1"
  local value="$2"
  if (( ${#value} > 255 )) || [[ ! "$value" =~ ^[A-Za-z0-9._:/-]*$ ]]; then
    echo "$name invalide (0-255 caractères parmi [A-Za-z0-9._:/-])." >&2
    exit 1
  fi
}
validate_boolean() {
  local name="$1"
  local value="$2"
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "$name doit valoir true ou false." >&2
    exit 1
  fi
}

if [[ ! "$GITHUB_SHA_INPUT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "github.sha invalide." >&2
  exit 1
fi

if [[ "$EVENT_NAME" == "workflow_dispatch" ]]; then
  if [[ "$EVAL_MODE_INPUT" != "smoke" && "$EVAL_MODE_INPUT" != "full" ]]; then
    echo "eval_mode invalide." >&2
    exit 1
  fi
  if [[ "$TARGET_ENVIRONMENT_INPUT" != "staging" && "$TARGET_ENVIRONMENT_INPUT" != "production" ]]; then
    echo "target_environment invalide." >&2
    exit 1
  fi
  validate_required_name "goldset_name" "$GOLDSET_NAME_INPUT"
  validate_required_name "tag" "$GOLDSET_TAG_INPUT"
  if [[ ! "$LIMIT_INPUT" =~ ^[0-9]+$ ]]; then
    echo "limit doit être un entier positif ou nul." >&2
    exit 1
  fi
  validate_boolean "skip_ragas" "$SKIP_RAGAS_INPUT"
  validate_boolean "skip_judge" "$SKIP_JUDGE_INPUT"
  validate_boolean "any_goldset" "$ANY_GOLDSET_INPUT"
  if [[ ! "$BASELINE_RUN_ID_INPUT" =~ ^[0-9]*$ ]]; then
    echo "baseline_run_id doit être un identifiant numérique." >&2
    exit 1
  fi
  validate_optional_label "baseline_run_label" "$BASELINE_RUN_LABEL_INPUT"
  if (( ${#RUN_LABEL_SUFFIX_INPUT} > 64 )) || [[ ! "$RUN_LABEL_SUFFIX_INPUT" =~ ^[A-Za-z0-9._-]*$ ]]; then
    echo "run_label_suffix invalide (0-64 caractères parmi [A-Za-z0-9._-])." >&2
    exit 1
  fi

  baseline_run_id="$BASELINE_RUN_ID_INPUT"
  baseline_run_label="$BASELINE_RUN_LABEL_INPUT"
  if [[ -z "$baseline_run_id" ]]; then
    baseline_run_id="$DEFAULT_BASELINE_RUN_ID"
  fi
  if [[ -z "$baseline_run_label" ]]; then
    baseline_run_label="$DEFAULT_BASELINE_RUN_LABEL"
  fi
  if [[ ! "$baseline_run_id" =~ ^[0-9]*$ ]]; then
    echo "RAG_QUALITY_BASELINE_RUN_ID doit être numérique." >&2
    exit 1
  fi
  validate_optional_label "RAG_QUALITY_BASELINE_RUN_LABEL" "$baseline_run_label"

  if [[ "$EVAL_MODE_INPUT" == "full" || "$TARGET_ENVIRONMENT_INPUT" == "production" ]]; then
    eval_mode=full
    limit=0
    compare_baseline=true
  else
    eval_mode=smoke
    limit="$LIMIT_INPUT"
    compare_baseline=false
  fi
  run_label="$TARGET_ENVIRONMENT_INPUT-$eval_mode-$GITHUB_SHA_INPUT${RUN_LABEL_SUFFIX_INPUT:+-$RUN_LABEL_SUFFIX_INPUT}"

  {
    printf 'GOLDSET_NAME=%s\n' "$GOLDSET_NAME_INPUT"
    printf 'GOLDSET_TAG=%s\n' "$GOLDSET_TAG_INPUT"
    printf 'EVAL_MODE=%s\n' "$eval_mode"
    printf 'COMPARE_BASELINE=%s\n' "$compare_baseline"
    printf 'LIMIT=%s\n' "$limit"
    printf 'SKIP_RAGAS=%s\n' "$SKIP_RAGAS_INPUT"
    printf 'SKIP_JUDGE=%s\n' "$SKIP_JUDGE_INPUT"
    printf 'ANY_GOLDSET=%s\n' "$ANY_GOLDSET_INPUT"
    printf 'BASELINE_RUN_ID=%s\n' "$baseline_run_id"
    printf 'BASELINE_RUN_LABEL=%s\n' "$baseline_run_label"
    printf 'RUN_LABEL=%s\n' "$run_label"
  } >> "$GITHUB_ENV"
else
  if [[ "$EVENT_NAME" != "push" || ( "$GITHUB_REF_NAME_INPUT" != "dev" && "$GITHUB_REF_NAME_INPUT" != "staging" ) ]]; then
    echo "Événement ou branche automatique non autorisé." >&2
    exit 1
  fi
  if [[ ! "$DEFAULT_BASELINE_RUN_ID" =~ ^[0-9]*$ ]]; then
    echo "RAG_QUALITY_BASELINE_RUN_ID doit être numérique." >&2
    exit 1
  fi
  validate_optional_label "RAG_QUALITY_BASELINE_RUN_LABEL" "$DEFAULT_BASELINE_RUN_LABEL"
  {
    printf 'GOLDSET_NAME=%s\n' "iteration2_V1"
    printf 'GOLDSET_TAG=%s\n' "iteration2"
    printf 'EVAL_MODE=%s\n' "smoke"
    printf 'COMPARE_BASELINE=%s\n' "false"
    printf 'LIMIT=%s\n' "5"
    printf 'SKIP_RAGAS=%s\n' "true"
    printf 'SKIP_JUDGE=%s\n' "false"
    printf 'ANY_GOLDSET=%s\n' "false"
    printf 'BASELINE_RUN_ID=%s\n' "$DEFAULT_BASELINE_RUN_ID"
    printf 'BASELINE_RUN_LABEL=%s\n' "$DEFAULT_BASELINE_RUN_LABEL"
    printf 'RUN_LABEL=%s\n' "$GITHUB_REF_NAME_INPUT-$GITHUB_SHA_INPUT"
  } >> "$GITHUB_ENV"
fi

# The protocol validates provider/model/vote inputs before emitting
# its own environment assignments.
uv run python .github/scripts/rag_quality_protocol.py >> "$GITHUB_ENV"
