"""Bounded aggregate evaluation metrics, without question/answer payloads."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

MAX_EVAL_RUNS = 50
EVAL_COLUMNS = (
    "id",
    "goldset_name",
    "status",
    "judge_model",
    "git_sha",
    "config_fingerprint",
    "created_at",
    "completed_at",
    "aggregate",
    "metadata",
    "tag_filter",
)
SCORES = (
    "doc_recall_avg",
    "doc_precision_avg",
    "hit_rate_avg",
    "mrr_avg",
    "judge_score_avg",
    "judge_pass_rate",
    "retrieval_gap_rate",
    "judge_legal_correctness_avg",
    "judge_completeness_avg",
    "judge_gold_answer_alignment_avg",
    "judge_source_support_avg",
    "ragas_faithfulness_avg",
    "ragas_context_precision_avg",
    "ragas_context_recall_avg",
)
COUNTS = ("total", "completed", "failed", "judge_failed", "ragas_failed")
STAGES = ("pool", "sections_top20", "sections_top12", "selector_kept", "context_builder_output")
PREFIX = "assistant_rh_rag_eval_"
EVAL_HELP = {
    PREFIX + "schema_available": "Whether the evaluation run schema is available.",
    PREFIX + "runs_exposed": "Number of recent evaluation runs in the bounded export window (at most 50).",
    PREFIX + "run_info": "Immutable metadata of a recorded evaluation; scope ID is not proof of baseline comparability.",
    PREFIX + "run_status": "Run execution status: 0 started, 1 completed, 2 failed, 3 failed quality gate, 4 other.",
    PREFIX + "run_timestamp_seconds": "Evaluation creation timestamp, not the Prometheus scrape timestamp.",
    PREFIX + "run_duration_seconds": "Recorded evaluation elapsed duration, available only after completion.",
    PREFIX + "run_questions": "Recorded evaluation question counts by result; absent values are not zero.",
    PREFIX + "run_score": "Recorded aggregate evaluation metric; never substitutes zero for unavailable scores.",
    PREFIX + "stage_score": "Recorded retrieval funnel metric by stage and attempt.",
    PREFIX + "stage_questions": "Number of items contributing a stage diagnostic (not necessarily every metric).",
    PREFIX + "baseline_status": "Stored comparison: 0 not requested, 1 passed, 2 failed, 3 not comparable, 4 missing baseline, 5 unknown.",
    PREFIX + "baseline_delta": "Recorded candidate minus baseline delta, emitted only when explicitly comparable.",
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def eval_run_metrics(row: dict[str, Any]) -> list[tuple[str, float, dict[str, str]]]:
    """Return allowlisted scalar metrics and bounded metadata for one stored run."""
    labels = {"run_id": str(row["id"]), "goldset": str(row.get("goldset_name") or "unknown")[:160]}
    aggregate = _dict(row.get("aggregate"))
    scope = _dict(row.get("eval_scope"))
    scope_id = hashlib.sha256(json.dumps({"scope": scope, "tags": row.get("tag_filter")}, sort_keys=True, default=str).encode()).hexdigest()[:16]
    info = {
        "judge_model": str(row.get("judge_model") or "unknown")[:160],
        "git_sha": str(row.get("git_sha") or "unknown")[:40],
        "config_id": str(row.get("config_fingerprint") or "unknown")[:64],
        "scope_id": scope_id,
        "limit": str(scope.get("limit", "unknown"))[:16],
    }
    samples = [(PREFIX + "run_info", 1.0, {**labels, **info})]

    def emit(suffix: str, raw: Any, **extra: str) -> None:
        value = _number(raw)
        if value is not None:
            samples.append((PREFIX + suffix, value, {**labels, **extra}))

    emit("run_status", {"started": 0, "completed": 1, "failed": 2, "failed_quality_gate": 3}.get(row.get("status"), 4))
    emit("run_timestamp_seconds", row.get("created_epoch"))
    duration = _number(row.get("duration_seconds"))
    if duration is not None and duration >= 0:
        emit("run_duration_seconds", duration)
    for key in COUNTS:
        value = _number(aggregate.get(key))
        if value is not None and value >= 0:
            emit("run_questions", value, result=key)
    for key in SCORES:
        emit("run_score", aggregate.get(key), metric=key)
    stages = _dict(aggregate.get("stage_metrics"))
    for attempt in ("initial", "selector_retry"):
        for stage in STAGES:
            values = _dict(_dict(stages.get(attempt)).get(stage))
            emit("stage_questions", values.get("n"), attempt=attempt, stage=stage)
            for key in ("hit_rate_avg", "doc_recall_avg"):
                emit("stage_score", values.get(key), attempt=attempt, stage=stage, metric=key)
    comparison = _dict(aggregate.get("baseline_comparison"))
    status = {"passed": 1, "failed": 2, "not_comparable": 3, "missing_baseline": 4}.get(comparison.get("status"), 5 if comparison else 0)
    emit("baseline_status", status)
    if comparison.get("comparable") is True and str(comparison.get("baseline_run_id") or "").isdigit():
        for key in ("judge_pass_rate", "doc_recall_avg"):
            emit(
                "baseline_delta",
                _dict(_dict(comparison.get("metrics")).get(key)).get("delta"),
                metric=key,
                baseline_run_id=str(comparison["baseline_run_id"]),
            )
    return samples
