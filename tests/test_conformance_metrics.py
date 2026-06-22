from __future__ import annotations

from assistant_rh_rag_pipeline.conformance import (
    _bool_match,
    _coerce_bool,
    aggregate_conformance,
    compare_query_runs,
    jaccard_similarity,
)


def test_jaccard_similarity_basic():
    assert jaccard_similarity(["a", "b"], ["b", "c"]) == 1 / 3


def test_coerce_bool_handles_real_and_stringified_values():
    assert _coerce_bool(True) is True
    assert _coerce_bool(False) is False
    assert _coerce_bool(None) is None
    # Stringified booleans (loosely-typed transport) must not collapse to True
    # the way bool("false") would.
    assert _coerce_bool("false") is False
    assert _coerce_bool("true") is True
    assert _coerce_bool("False") is False
    assert _coerce_bool(0) is False
    assert _coerce_bool(1) is True
    assert _coerce_bool("garbage") is None


def test_bool_match_three_state():
    assert _bool_match(True, True) is True
    assert _bool_match(True, False) is False
    # Real agreement reported correctly even when one side is stringified.
    assert _bool_match(False, "false") is True
    assert _bool_match(True, "false") is False
    # Missing side → not comparable.
    assert _bool_match(None, True) is None


def test_compare_query_runs_llm_match_isolates_heuristic():
    """needs_legal_search_match (merged) vs needs_legal_search_llm_match (LLM-only)."""
    py_run = {
        "answer": "x",
        "timing": {"pipeline_total_ms": 100},
        "metadata": {
            # Python heuristic forced True; the LLM itself said False.
            "needs_legal_search": True,
            "needs_legal_search_llm": False,
        },
    }
    ca_run = {
        "answer": "x",
        "timing": {"pipeline_total_ms": 100},
        # Candidate (no heuristic) agrees with the LLM: False.
        "metadata": {"needs_legal_search": False},
    }
    result = compare_query_runs(query_id="q", python_run=py_run, candidate_run=ca_run)
    # Merged decisions diverge (heuristic-driven) ...
    assert result.needs_legal_search_match is False
    # ... but the underlying LLM classifiers actually agree.
    assert result.needs_legal_search_llm_match is True


def test_compare_query_runs_extracts_stage_overlaps():
    py_run = {
        "answer": "Le CDD est limité.",
        "timing": {"pipeline_total_ms": 1000},
        "metadata": {
            "intent": "rag_query",
            "theme": "typologie_contrats",
            "needs_legal_search": False,
            "retrieved_chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}],
            "aggregated_sections": [{"section_id": "s1"}, {"section_id": "s2"}],
            "context_items_ref": [{"section_id": "s1"}],
        },
    }
    ca_run = {
        "answer": "Le CDD est limité dans le temps.",
        "timing": {"pipeline_total_ms": 1200},
        "metadata": {
            "intent": "rag_query",
            "theme": "typologie_contrats",
            "needs_legal_search": False,
            "retrieved_chunks": [{"chunk_id": "c2"}, {"chunk_id": "c3"}],
            "aggregated_sections": [{"section_id": "s2"}, {"section_id": "s4"}],
            "context_items_ref": [{"section_id": "s1"}],
        },
    }

    result = compare_query_runs(query_id="q1", python_run=py_run, candidate_run=ca_run, top_k=2)
    assert result.intent_match is True
    assert result.theme_match is True
    assert result.needs_legal_search_match is True
    assert result.retrieval_overlap_topk == 1 / 3
    assert result.section_overlap_topk == 1 / 3
    assert result.context_overlap_topk == 1.0
    assert result.latency_ratio == 1.2


def test_compare_query_runs_handles_non_dict_metadata():
    result = compare_query_runs(
        query_id="q-malformed-meta",
        python_run={"answer": "test", "timing": {"pipeline_total_ms": 100}, "metadata": "unexpected"},
        candidate_run={"answer": "test", "timing": {"pipeline_total_ms": 120}, "metadata": ["unexpected"]},
    )

    assert result.intent_match is None
    assert result.theme_match is None
    assert result.needs_legal_search_match is None
    assert result.retrieval_overlap_topk is None
    assert result.section_overlap_topk is None
    assert result.context_overlap_topk is None


def test_compare_query_runs_ignores_non_dict_ranked_entries():
    py_run = {
        "answer": "alpha",
        "timing": {"pipeline_total_ms": 100},
        "metadata": {
            "retrieved_chunks": ["bad", {"chunk_id": "c1"}, 42],
            "aggregated_sections": [],
            "context_items_ref": [],
        },
    }
    ca_run = {
        "answer": "alpha",
        "timing": {"pipeline_total_ms": 100},
        "metadata": {
            "retrieved_chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}],
            "aggregated_sections": [],
            "context_items_ref": [],
        },
    }

    result = compare_query_runs(query_id="q-ranked", python_run=py_run, candidate_run=ca_run, top_k=3)
    assert result.retrieval_overlap_topk == 0.5


def test_aggregate_conformance():
    rows = [
        compare_query_runs(
            query_id="q1",
            python_run={"answer": "a b", "timing": {"pipeline_total_ms": 100}, "metadata": {}},
            candidate_run={"answer": "a", "timing": {"pipeline_total_ms": 120}, "metadata": {}},
        ),
        compare_query_runs(
            query_id="q2",
            python_run={"answer": "x y", "timing": {"pipeline_total_ms": 200}, "metadata": {}},
            candidate_run={"answer": "x z", "timing": {"pipeline_total_ms": 180}, "metadata": {}},
        ),
    ]
    agg = aggregate_conformance(rows)
    assert agg["n_queries"] == 2
    assert agg["answer_token_jaccard_avg"] is not None
    assert agg["latency_ratio_avg"] is not None
