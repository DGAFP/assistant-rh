"""Tests du feedback analyzer : robustesse JSON + attribution mécanique d'étage.

Couvre les deux régressions observées sur la campagne beta de juillet 2026 :
17 analyses sur 25 crashées (JSON tronqué par max_tokens, content=null) et
verdicts attribués au mauvais étage faute de visibilité sur le pool brut.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from assistant_rh_rag_pipeline import feedback_analyzer as fa


# ---------------------------------------------------------------------------
# _extract_json – réparation des sorties LLM
# ---------------------------------------------------------------------------
class TestExtractJson:
    def test_plain_object(self):
        assert fa._extract_json('{"error_category": "retrieval_issue", "short_reason": "ok"}') == {
            "error_category": "retrieval_issue",
            "short_reason": "ok",
        }

    def test_json_code_fence(self):
        raw = 'Voici :\n```json\n{"error_category": "other", "short_reason": "x"}\n```\nfin'
        assert fa._extract_json(raw)["error_category"] == "other"

    def test_prose_around_object(self):
        raw = 'Analyse : {"error_category": "chunk_quality", "short_reason": "y"} — voilà.'
        assert fa._extract_json(raw)["error_category"] == "chunk_quality"

    def test_truncated_string_is_repaired(self):
        # Coupé en plein vol par max_tokens (cas « Unterminated string »)
        raw = '{"error_category": "generator_incomplete", "short_reason": "Le générateur a omis les mont'
        data = fa._extract_json(raw)
        assert data["error_category"] == "generator_incomplete"
        assert data["short_reason"].startswith("Le générateur")

    def test_truncated_after_comma(self):
        raw = '{"error_category": "retrieval_issue",'
        assert fa._extract_json(raw)["error_category"] == "retrieval_issue"

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            fa._extract_json("Je ne peux pas répondre en JSON.")


# ---------------------------------------------------------------------------
# _call_albert_json – content nul / clé absente
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, message):
        self._message = message

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": self._message}]}


class TestCallAlbertJson:
    def test_reasoning_content_fallback(self, monkeypatch):
        monkeypatch.setenv("ALBERT_API_KEY", "test-key")
        payload = json.dumps({"missing_info": "x", "markers": ["159,20"]})
        monkeypatch.setattr(
            fa.requests,
            "post",
            lambda *a, **k: _FakeResp({"content": None, "reasoning_content": payload}),
        )
        assert fa._call_albert_json("s", "u")["markers"] == ["159,20"]

    def test_empty_content_raises_after_retries(self, monkeypatch):
        monkeypatch.setenv("ALBERT_API_KEY", "test-key")
        calls = []
        monkeypatch.setattr(
            fa.requests,
            "post",
            lambda *a, **k: calls.append(1) or _FakeResp({"content": None}),
        )
        with pytest.raises(RuntimeError):
            fa._call_albert_json("s", "u", retries=1)
        assert len(calls) == 2  # retry effectif

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ALBERT_API_KEY", raising=False)
        with pytest.raises(RuntimeError):
            fa._call_albert_json("s", "u")

    def test_call_albert_returns_other_on_failure(self, monkeypatch):
        monkeypatch.delenv("ALBERT_API_KEY", raising=False)
        category, reason = fa._call_albert("s", "u")
        assert category == "other"
        assert reason.startswith("Erreur analyse:")

    def test_call_albert_rejects_unknown_category(self, monkeypatch):
        monkeypatch.setenv("ALBERT_API_KEY", "test-key")
        payload = json.dumps({"error_category": "pas_une_categorie", "short_reason": "z"})
        monkeypatch.setattr(fa.requests, "post", lambda *a, **k: _FakeResp({"content": payload}))
        category, _ = fa._call_albert("s", "u")
        assert category == "other"


# ---------------------------------------------------------------------------
# _normalize / _stage_flags – détection de marqueurs par étage
# ---------------------------------------------------------------------------
class TestStageFlags:
    def test_normalize_apostrophes_case_nbsp(self):
        assert fa._normalize("Plafond d’Emploi !") == "plafond d'emploi !"

    def test_marker_found_in_pool_only(self):
        feedback = {
            "v3_chunks_raw": '[{"text": "montant de 159,20 € par semaine"}]',
            "retrieved_chunks": [{"text": "autre chose"}],
            "served_sources": "[]",
            "rag_prompt": "contexte sans montants",
        }
        flags = fa._stage_flags(feedback, ["159,20"])
        assert flags == {
            "pool": True,
            "selector_input": False,
            "served": False,
            "generator_context": False,
        }

    def test_marker_matches_through_curly_apostrophe(self):
        feedback = {
            "v3_chunks_raw": "le plafond d’emploi est fixé",
            "retrieved_chunks": [],
            "served_sources": "",
            "rag_prompt": "",
        }
        assert fa._stage_flags(feedback, ["plafond d'emploi"])["pool"] is True


# ---------------------------------------------------------------------------
# _classify_from_flags – arbre de décision
# ---------------------------------------------------------------------------
class TestClassifyFromFlags:
    @staticmethod
    def _flags(pool, selector_input, generator_context):
        return {
            "pool": pool,
            "selector_input": selector_input,
            "served": False,
            "generator_context": generator_context,
        }

    def test_missing_document_when_not_in_corpus(self):
        cat, reason = fa._classify_from_flags(False, self._flags(False, False, False), True, ["annexe 3"], "montants DDI")
        assert cat == "missing_document"
        assert "corpus" in reason

    def test_retrieval_issue_when_in_corpus_but_not_in_pool(self):
        cat, _ = fa._classify_from_flags(True, self._flags(False, False, False), True, ["m"], "")
        assert cat == "retrieval_issue"

    def test_candidate_cut_when_lost_between_pool_and_selector(self):
        cat, _ = fa._classify_from_flags(True, self._flags(True, False, False), True, ["m"], "")
        assert cat == "candidate_cut"

    def test_selector_wrong_priority_when_selector_dropped_it(self):
        cat, _ = fa._classify_from_flags(True, self._flags(True, True, False), True, ["m"], "")
        assert cat == "selector_wrong_priority"

    def test_candidate_cut_without_selector(self):
        cat, _ = fa._classify_from_flags(True, self._flags(True, True, False), False, ["m"], "")
        assert cat == "candidate_cut"

    def test_generator_family_returns_none(self):
        cat, reason = fa._classify_from_flags(True, self._flags(True, True, True), True, ["m"], "")
        assert cat is None
        assert reason == ""

    def test_candidate_cut_is_a_valid_category(self):
        assert "candidate_cut" in fa.ALL_VALID_CATEGORIES
