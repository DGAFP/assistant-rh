"""Conformance helpers for Python reference versus candidate comparisons.

This module is intentionally lightweight and dependency-free so it can be
used in scripts and CI runners.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles
from typing import Any, Iterable


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    """Normalize a metadata flag to a real bool.

    Tolerates JSON-stringified booleans ("true"/"false") that can appear when
    metadata round-trips through a loosely-typed transport — a plain
    ``bool("false")`` would wrongly be ``True``. Returns None for missing or
    unrecognized values so the caller can treat it as "not comparable".
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", ""):
            return False
    return None


def _bool_match(a: Any, b: Any) -> bool | None:
    """Three-state equality on two boolean-ish flags; None if either is absent."""
    ca = _coerce_bool(a)
    cb = _coerce_bool(b)
    if ca is None or cb is None:
        return None
    return ca == cb


def normalize_tokens(text: str) -> list[str]:
    """Normalize answer text into lowercase whitespace tokens."""
    return (text or "").lower().split()


def jaccard_similarity(left: Iterable[str], right: Iterable[str]) -> float | None:
    """Set-based Jaccard similarity. Returns None when both sides are empty."""
    a = set(left)
    b = set(right)
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


def _as_ranked_ids(entries: list[dict[str, Any]], key: str, top_k: int) -> list[str]:
    ids: list[str] = []
    if not isinstance(entries, list):
        return ids

    for entry in entries[:top_k]:
        if not isinstance(entry, dict):
            continue

        value = str(entry.get(key, "") or "").strip()
        if value:
            ids.append(value)
    return ids


def ranked_overlap_jaccard(
    left_entries: list[dict[str, Any]],
    right_entries: list[dict[str, Any]],
    *,
    key: str,
    top_k: int,
) -> float | None:
    """Compute top-k overlap as Jaccard on ranked ids."""
    left_ids = _as_ranked_ids(left_entries, key=key, top_k=top_k)
    right_ids = _as_ranked_ids(right_entries, key=key, top_k=top_k)
    return jaccard_similarity(left_ids, right_ids)


@dataclass
class QueryConformance:
    query_id: str
    answer_token_jaccard: float | None
    answer_length_ratio: float | None
    intent_match: bool | None
    theme_match: bool | None
    needs_legal_search_match: bool | None
    # LLM-only comparison: Python's pre-heuristic `needs_legal_search_llm`
    # vs the candidate's `needs_legal_search`. Separates LLM agreement
    # from heuristic-driven divergence.
    needs_legal_search_llm_match: bool | None
    retrieval_overlap_topk: float | None
    section_overlap_topk: float | None
    context_overlap_topk: float | None
    latency_ratio: float | None


def compare_query_runs(
    *,
    query_id: str,
    python_run: dict[str, Any],
    candidate_run: dict[str, Any],
    top_k: int = 10,
) -> QueryConformance:
    """Compare one Python run vs one candidate run.

    Both runs are expected to expose a ``metadata`` object with stage refs when
    available.
    """
    py_answer = str(python_run.get("answer", "") or "")
    ca_answer = str(candidate_run.get("answer", "") or "")

    py_meta = python_run.get("metadata")
    if not isinstance(py_meta, dict):
        py_meta = {}

    ca_meta = candidate_run.get("metadata")
    if not isinstance(ca_meta, dict):
        ca_meta = {}

    py_tokens = normalize_tokens(py_answer)
    ca_tokens = normalize_tokens(ca_answer)

    answer_token_jaccard = jaccard_similarity(py_tokens, ca_tokens)

    answer_length_ratio = None
    if py_answer:
        answer_length_ratio = len(ca_answer) / len(py_answer)

    py_intent = py_meta.get("intent")
    ca_intent = ca_meta.get("intent")
    intent_match = None if py_intent is None or ca_intent is None else py_intent == ca_intent

    py_theme = py_meta.get("theme")
    ca_theme = ca_meta.get("theme")
    theme_match = None if py_theme is None or ca_theme is None else py_theme == ca_theme

    ca_needs_legal = ca_meta.get("needs_legal_search")
    # End-to-end (merged) gating parity: Python's FINAL needs_legal_search
    # (LLM ∪ deterministic heuristic) vs the candidate's. Against an LLM-only
    # candidate, a mismatch here legitimately reflects
    # heuristic-driven divergence, NOT an LLM disagreement — read
    # needs_legal_search_llm_match below to isolate whether the underlying
    # classifiers actually disagree.
    needs_legal_search_match = _bool_match(py_meta.get("needs_legal_search"), ca_needs_legal)

    # LLM-only parity: Python's pre-heuristic decision vs the candidate's. This
    # factors out the Python guardrail so the metric tracks classifier agreement
    # rather than dropping every time the heuristic forces a True the LLM didn't.
    needs_legal_search_llm_match = _bool_match(py_meta.get("needs_legal_search_llm"), ca_needs_legal)

    retrieval_overlap_topk = ranked_overlap_jaccard(
        py_meta.get("retrieved_chunks") or [],
        ca_meta.get("retrieved_chunks") or [],
        key="chunk_id",
        top_k=top_k,
    )
    section_overlap_topk = ranked_overlap_jaccard(
        py_meta.get("aggregated_sections") or [],
        ca_meta.get("aggregated_sections") or [],
        key="section_id",
        top_k=top_k,
    )
    context_overlap_topk = ranked_overlap_jaccard(
        py_meta.get("context_items_ref") or [],
        ca_meta.get("context_items_ref") or [],
        key="section_id",
        top_k=top_k,
    )

    py_latency = _safe_float((python_run.get("timing") or {}).get("pipeline_total_ms"))
    ca_latency = _safe_float((candidate_run.get("timing") or {}).get("pipeline_total_ms"))
    latency_ratio = None
    if py_latency and py_latency > 0 and ca_latency is not None:
        latency_ratio = ca_latency / py_latency

    return QueryConformance(
        query_id=query_id,
        answer_token_jaccard=answer_token_jaccard,
        answer_length_ratio=answer_length_ratio,
        intent_match=intent_match,
        theme_match=theme_match,
        needs_legal_search_match=needs_legal_search_match,
        needs_legal_search_llm_match=needs_legal_search_llm_match,
        retrieval_overlap_topk=retrieval_overlap_topk,
        section_overlap_topk=section_overlap_topk,
        context_overlap_topk=context_overlap_topk,
        latency_ratio=latency_ratio,
    )


def aggregate_conformance(items: list[QueryConformance]) -> dict[str, Any]:
    """Aggregate query-level conformance into summary metrics."""

    def _avg(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return None if not present else sum(present) / len(present)

    def _match_rate(values: list[bool | None]) -> float | None:
        present = [v for v in values if v is not None]
        if not present:
            return None
        return sum(1 for v in present if v) / len(present)

    latency_values = [i.latency_ratio for i in items if i.latency_ratio is not None]
    latency_ratio_p95 = None
    if len(latency_values) >= 2:
        latency_ratio_p95 = quantiles(latency_values, n=20)[18]
    elif len(latency_values) == 1:
        latency_ratio_p95 = latency_values[0]

    return {
        "n_queries": len(items),
        "answer_token_jaccard_avg": _avg([i.answer_token_jaccard for i in items]),
        "answer_length_ratio_avg": _avg([i.answer_length_ratio for i in items]),
        "intent_match_rate": _match_rate([i.intent_match for i in items]),
        "theme_match_rate": _match_rate([i.theme_match for i in items]),
        "needs_legal_search_match_rate": _match_rate([i.needs_legal_search_match for i in items]),
        "needs_legal_search_llm_match_rate": _match_rate([i.needs_legal_search_llm_match for i in items]),
        "retrieval_overlap_topk_avg": _avg([i.retrieval_overlap_topk for i in items]),
        "section_overlap_topk_avg": _avg([i.section_overlap_topk for i in items]),
        "context_overlap_topk_avg": _avg([i.context_overlap_topk for i in items]),
        "latency_ratio_avg": _avg([i.latency_ratio for i in items]),
        "latency_ratio_p95": latency_ratio_p95,
    }
