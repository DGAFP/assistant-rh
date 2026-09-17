"""Recorded question diagnostics, bounded by run; no RAG replay or alias resolution."""

from __future__ import annotations

from typing import Any

from assistant_rh_data_engineering.jobs.rag_eval_metrics import PREFIX, STAGES, _dict, _number

MAX_EVAL_ITEMS_PER_RUN = 200
ITEM_COLUMNS = ("id", "run_id", "question_id", "question", "gold_sources", "deterministic_metrics", "judge_result")
ATTEMPTS = ("initial", "selector_retry")
# Top-12 is an alternative diagnostic cut, not the input to the selector.
TRANSITIONS = (("pool", "sections_top20"), ("sections_top20", "selector_kept"), ("selector_kept", "context_builder_output"))
ITEM_HELP = {
    PREFIX + "items_schema_available": "Whether recorded question diagnostics can be read.",
    PREFIX + "items_status": "Detail availability: 0 empty, 1 complete, 2 exceeds the 200-item limit; oversized runs are not partially summarized.",
    PREFIX + "items_exposed": "Number of recorded question items exported for a run.",
    PREFIX + "item_info": "Bounded recorded question and expected source excerpts; item_id uniquely identifies a stored item.",
    PREFIX + "item_judge_pass": "Stored judge verdict: 1 pass, 0 fail, -1 unavailable.",
    PREFIX + "item_judge_score": "Stored judge score, absent when unavailable.",
    PREFIX + "item_stage_state": "Gold presence: 1 found, 0 absent, -1 stage untraced, -2 no gold, -3 invalid hit measurement.",
    PREFIX + "item_stage_recall": "Recorded recall for this question and stage, using historical identifier matching.",
    PREFIX + "funnel_questions": "Per-stage counts with separate hit and recall denominators; missing stages never become misses.",
    PREFIX + "funnel_score": "Mean of finite recorded question metrics at the selected stage and attempt, not the legacy union metric.",
    PREFIX + "transition_questions": "Paired question counts across adjacent pipeline stages; excludes unmeasured endpoints.",
}


def _excerpt(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ratio(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 1 else None


def _stage(item: dict, attempt: str, stage: str) -> tuple[int, float | None]:
    metrics = _dict(item.get("deterministic_metrics"))
    value = _dict(_dict(metrics.get("stages")).get(attempt)).get(stage)
    if not isinstance(value, dict):
        return -1, None
    if _number(metrics.get("gold_count")) == 0:
        return -2, None
    hit = _ratio(value.get("hit_rate"))
    state = int(hit) if hit in (0, 1) else -3
    return state, _ratio(value.get("doc_recall"))


def eval_item_metrics(run: dict[str, Any], items: list[dict[str, Any]]) -> list[tuple[str, float, dict[str, str]]]:
    """Keep item identity stable and compute averages only over measured endpoints."""
    base = {"run_id": str(run["id"]), "goldset": str(run.get("goldset_name") or "unknown")[:160]}
    samples: list[tuple[str, float, dict[str, str]]] = []

    def emit(name: str, value: float, **labels: str) -> None:
        samples.append((PREFIX + name, value, {**base, **labels}))

    oversized = len(items) > MAX_EVAL_ITEMS_PER_RUN
    emit("items_status", 2 if oversized else int(bool(items)))
    emit("items_exposed", 0 if oversized else len(items))
    if not items or oversized:
        return samples
    for item in items:
        labels = {"item_id": str(item["id"])}
        sources = item.get("gold_sources")
        emit(
            "item_info",
            1,
            **labels,
            question_id=str(item.get("question_id") if item.get("question_id") is not None else "unknown"),
            question=_excerpt(item.get("question"), 500),
            gold_sources=_excerpt(" · ".join(str(s) for s in sources[:20]) if isinstance(sources, list) else "", 500),
        )
        judge = _dict(item.get("judge_result"))
        verdict = judge.get("pass")
        completed = judge.get("status") == "completed"
        emit("item_judge_pass", int(verdict) if completed and isinstance(verdict, bool) else -1, **labels)
        score = _ratio(judge.get("score"))
        if completed and score is not None:
            emit("item_judge_score", score, **labels)
        for attempt in ATTEMPTS:
            # Emit the initial attempt even when untraced; retry is optional.
            if attempt != "initial" and attempt not in _dict(_dict(item.get("deterministic_metrics")).get("stages")):
                continue
            for stage in STAGES:
                state, recall = _stage(item, attempt, stage)
                stage_labels = {**labels, "attempt": attempt, "stage": stage}
                emit("item_stage_state", state, **stage_labels)
                if recall is not None:
                    emit("item_stage_recall", recall, **stage_labels)
    for attempt in ATTEMPTS:
        for stage in STAGES:
            values = [_stage(item, attempt, stage) for item in items]
            hits = [state for state, _ in values if state >= 0]
            recalls = [recall for _, recall in values if recall is not None]
            labels = {"attempt": attempt, "stage": stage, "stage_order": str(STAGES.index(stage) + 1)}
            counts = {
                "total": len(items),
                "hit": sum(hits),
                "hit_measured": len(hits),
                "recall_measured": len(recalls),
                "trace_missing": sum(state == -1 for state, _ in values),
                "no_gold": sum(state == -2 for state, _ in values),
                "invalid": sum(state == -3 for state, _ in values),
            }
            for measure, count in counts.items():
                emit("funnel_questions", count, **labels, measure=measure)
            if hits:
                emit("funnel_score", sum(hits) / len(hits), **labels, metric="hit_rate")
            if recalls:
                emit("funnel_score", sum(recalls) / len(recalls), **labels, metric="doc_recall")
        for before, after in TRANSITIONS:
            paired = [(_stage(i, attempt, before)[0], _stage(i, attempt, after)[0]) for i in items]
            paired = [(a, b) for a, b in paired if a >= 0 and b >= 0]
            counts = {
                "paired": len(paired),
                "lost": sum(a == 1 and b == 0 for a, b in paired),
                "gained": sum(a == 0 and b == 1 for a, b in paired),
                "retained": sum(a == b == 1 for a, b in paired),
            }
            for measure, count in counts.items():
                emit(
                    "transition_questions",
                    count,
                    attempt=attempt,
                    transition=f"{before}_to_{after}",
                    transition_order=str(TRANSITIONS.index((before, after)) + 1),
                    measure=measure,
                )
    return samples
