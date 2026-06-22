"""Assertion runner for tests/conformance/queries.legifrance-source-check.jsonl.

Each row in the JSONL is loaded as a pytest case. We mock the LLM classifier
so the test is hermetic — the goal is to validate the QueryProcessor heuristic
(intent gating + legal-search guardrail) against the fixture's `expected_*`
fields. The DGAFP retrieval and refusal expectations are documented as
metadata only; assertion against the live RAG pipeline belongs in a separate
integration runner (out of scope for unit tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from assistant_rh_rag_pipeline.config import QueryProcessorConfig
from assistant_rh_rag_pipeline.query_processor import Intent, QueryProcessor

_FIXTURE_PATH = Path(__file__).parent / "conformance" / "queries.legifrance-source-check.jsonl"


def _load_cases() -> list[dict]:
    if not _FIXTURE_PATH.exists():
        return []
    cases: list[dict] = []
    for line in _FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases


_CASES = _load_cases()
_CASE_IDS = [c["id"] for c in _CASES]


def _intent_for(expected_intent: str) -> Intent:
    """Translate the JSONL string into the Intent enum."""
    mapping = {
        "rag_query": Intent.RAG_QUERY,
        "out_of_scope": Intent.OUT_OF_SCOPE,
        "chit_chat": Intent.CHIT_CHAT,
        "clarification": Intent.CLARIFICATION,
        "follow_up": Intent.FOLLOW_UP,
        "document_request": Intent.DOCUMENT_REQUEST,
    }
    if expected_intent not in mapping:
        raise ValueError(f"Unknown expected_intent: {expected_intent}")
    return mapping[expected_intent]


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
@patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
@patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
def test_legifrance_jsonl_case(mock_classify, _mock_acronyms, case):
    """One assertion per JSONL row.

    The LLM is mocked to return `needs_legal_search=False` for nominal cases
    (so the heuristic is the decisive signal) and to return the `expected_intent`
    so the dataclass.intent assertion checks the routing rather than the
    classifier's recall.
    """
    expected_intent = _intent_for(case.get("expected_intent", "rag_query"))
    mock_classify.return_value = {
        "intent": expected_intent,
        "confidence": 0.95,
        "reasoning": "JSONL fixture",
        "needs_legal": False,  # let the heuristic do the work
        "theme": None,
        "enriched_query": "",
        "query_for_retrieval": None,
        "direct_response": None,
        "raw": "{}",
        "classify_ok": True,
    }

    proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))
    result = proc.process(case["query"])

    expected_needs_legal = case.get("expected_needs_legal_search")
    if expected_needs_legal is not None:
        assert result.needs_legal_search is expected_needs_legal, (
            f"[{case['id']}] needs_legal_search expected={expected_needs_legal} got={result.needs_legal_search}"
        )

    assert result.intent == expected_intent, f"[{case['id']}] intent mismatch"
