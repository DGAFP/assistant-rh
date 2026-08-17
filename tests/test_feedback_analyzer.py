"""Tests du feedback analyzer : robustesse JSON + attribution mécanique d'étage.

Couvre les deux régressions observées sur la campagne beta de juillet 2026 :
17 analyses sur 25 crashées (JSON tronqué par max_tokens, content=null) et
verdicts attribués au mauvais étage faute de visibilité sur le pool brut.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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

    def test_call_albert_propagates_failure(self, monkeypatch):
        monkeypatch.delenv("ALBERT_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ALBERT_API_KEY"):
            fa._call_albert("s", "u")

    def test_call_albert_rejects_unknown_category(self, monkeypatch):
        monkeypatch.setenv("ALBERT_API_KEY", "test-key")
        payload = json.dumps({"error_category": "pas_une_categorie", "short_reason": "z"})
        monkeypatch.setattr(fa.requests, "post", lambda *a, **k: _FakeResp({"content": payload}))
        with pytest.raises(ValueError, match="catégorie Albert invalide"):
            fa._call_albert("s", "u")


# ---------------------------------------------------------------------------
# _resolve_chunk_content – traces réelles et texte complet
# ---------------------------------------------------------------------------
class _FakeConnection:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params):
        sql = str(query)
        table = next(table for table in self.rows_by_table if f"FROM {table} " in sql)
        rows = self.rows_by_table[table]
        return [SimpleNamespace(cid=chunk_id, chunk_text=rows[chunk_id]) for chunk_id in params["ids"] if chunk_id in rows]


class _FakeEngine:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def connect(self):
        return _FakeConnection(self.rows_by_table)


class TestResolveChunkContent:
    @pytest.mark.parametrize(
        ("ministry", "publisher", "table"),
        [
            ("matte", "Ministère de l'Aménagement du territoire et de la Transition écologique", "rag_chunks_matte"),
            ("mso", "Ministères sociaux", "rag_chunks_mso"),
            ("mi", "Ministère de l'Intérieur", "rag_chunks_mi"),
            ("masa", "Ministère de l'Agriculture et de la Souveraineté alimentaire", "rag_chunks_masa"),
        ],
    )
    def test_resolves_live_ministry_publisher_labels(self, ministry, publisher, table):
        engine = _FakeEngine({table: {"c1": "texte complet du chunk"}})

        resolved = fa._resolve_chunk_content(
            engine,
            [{"chunk_id": "c1", "table": publisher, "chunk_markdown": "preview"}],
            selected_ministry=ministry,
        )

        assert resolved[0]["text"] == "texte complet du chunk"
        assert resolved[0]["_content_resolved"] is True
        assert fa._chunk_refs_complete(resolved) is True

    def test_resolves_raw_pool_beyond_300_character_preview(self):
        marker = "159,20"
        full_text = ("x" * 350) + marker
        engine = _FakeEngine({"rag_chunks_mso": {"c1": full_text}})
        raw_pool = [
            {
                "chunk_id": "c1",
                "doc_publisher": "Ministères sociaux",
                "chunk_markdown": full_text[:300],
            }
        ]

        resolved = fa._resolve_chunk_content(engine, raw_pool, selected_ministry="mso")
        flags = fa._stage_flags({"v3_chunks_raw": resolved}, [marker])

        assert flags["pool"] is True

    def test_unresolved_ref_is_marked_incomplete(self):
        engine = _FakeEngine({"rag_chunks_mi": {}})
        resolved = fa._resolve_chunk_content(
            engine,
            [{"chunk_id": "missing", "table": "Ministère de l'Intérieur"}],
            selected_ministry="mi",
        )

        assert resolved[0]["text"] == ""
        assert resolved[0]["_content_resolved"] is False
        assert fa._chunk_refs_complete(resolved) is False

    @pytest.mark.parametrize("malformed", [None, "not-json", {"chunk_id": "c1"}, [42], [{}], [{"chunk_id": "c1"}]])
    def test_malformed_trace_is_incomplete(self, malformed):
        assert fa._chunk_refs_complete(malformed) is False

    def test_text_only_ref_is_complete(self):
        assert fa._chunk_refs_complete([{"text": "texte complet"}]) is True


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

    def test_question_is_not_treated_as_generator_context(self):
        feedback = {
            "rag_prompt": (
                "Voici le contexte documentaire pour repondre a la question :\n\n"
                "Le contexte ne contient pas le terme attendu.\n\n"
                "---\n\n"
                "**Question de l'utilisateur :** Quel est le montant du RIFSEEP ?"
            )
        }

        assert fa._stage_flags(feedback, ["RIFSEEP"])["generator_context"] is False

    def test_marker_in_documentary_context_is_detected(self):
        feedback = {
            "rag_prompt": (
                "Voici le contexte documentaire pour repondre a la question :\n\n"
                "Le RIFSEEP comprend deux parts.\n\n"
                "---\n\n"
                "**Question de l'utilisateur :** Comment fonctionne le régime indemnitaire ?"
            )
        }

        assert fa._stage_flags(feedback, ["RIFSEEP"])["generator_context"] is True

    def test_unparseable_generator_prompt_is_incomplete(self):
        assert fa._generator_context_from_prompt("ancien format sans délimiteur") is None

    def test_question_cannot_inject_a_second_context_delimiter(self):
        feedback = {
            "rag_prompt": (
                "Voici le contexte documentaire pour repondre a la question :\n\n"
                "Le contexte ne contient pas le terme attendu.\n\n"
                "---\n\n"
                "**Question de l'utilisateur :** Que signifie "
                "**Question de l'utilisateur :** RIFSEEP ?"
            )
        }

        assert fa._generator_context_from_prompt(feedback["rag_prompt"]) is None
        assert fa._stage_flags(feedback, ["RIFSEEP"])["generator_context"] is False

    def test_normalize_typographic_dashes(self):
        # Cas réel : marqueur « Cas‑de‑dispense_v3 » (U+2011) vs corpus ASCII
        assert fa._normalize("Cas‑de‑dispense_v3") == "cas-de-dispense_v3"


# ---------------------------------------------------------------------------
# _filter_hallucinated_markers – références inventées
# ---------------------------------------------------------------------------
class TestFilterHallucinatedMarkers:
    FEEDBACK = {
        "question": "montant des astreintes ?",
        "answer": "Le décret n° 2015-415 renvoie à un arrêté.",
        "comment": "il manque l'annexe 3",
        "reasons_negative": "Incomplet",
    }

    def test_drops_invented_reference(self):
        markers = ["décret n° 2020-1234", "159,20"]
        assert fa._filter_hallucinated_markers(markers, self.FEEDBACK) == ["159,20"]

    def test_keeps_reference_grounded_in_conversation(self):
        markers = ["décret n° 2015-415", "annexe 3"]
        assert fa._filter_hallucinated_markers(markers, self.FEEDBACK) == markers

    def test_keeps_non_reference_markers(self):
        markers = ["plafond d'emploi", "annexe 3"]
        assert fa._filter_hallucinated_markers(markers, self.FEEDBACK) == markers

    def test_extract_markers_deduplicates_normalized_values(self, monkeypatch):
        monkeypatch.setattr(
            fa,
            "_call_albert_json",
            lambda *args, **kwargs: {
                "missing_info": "régime indemnitaire",
                "markers": ["RIFSEEP", " rifseep ", "plafond d’emploi", "plafond d'emploi"],
            },
        )

        _missing_info, markers = fa._extract_markers(self.FEEDBACK)

        assert markers == ["RIFSEEP", "plafond d’emploi"]


class TestSearchCorpus:
    def test_unknown_ministry_returns_unknown_without_querying(self):
        class _NoQueryEngine:
            def connect(self):
                raise AssertionError("the database must not be queried without a complete scope")

        assert fa._search_corpus(_NoQueryEngine(), None, ["RIFSEEP"]) is None
        assert fa._search_corpus(_NoQueryEngine(), "unknown", ["RIFSEEP"]) is None

    @staticmethod
    def _engine_with_authoritative_text(authoritative_text):
        class _Result:
            def __init__(self, found):
                self.found = found

            def first(self):
                return object() if self.found else None

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, query, params):
                sql = str(query)
                assert "chunk_text ILIKE" in sql
                assert " OR text ILIKE" not in sql
                marker = params["p"].strip("%")
                return _Result(marker in authoritative_text)

        class _Engine:
            def connect(self):
                return _Connection()

        return _Engine()

    def test_generated_summary_text_does_not_establish_corpus_presence(self):
        engine = self._engine_with_authoritative_text("Le texte juridique ne contient pas le montant attendu.")

        assert fa._search_corpus(engine, "matte", ["159,20 euros"]) is False

    def test_authoritative_chunk_text_establishes_corpus_presence(self):
        engine = self._engine_with_authoritative_text("Le montant est de 159,20 euros par semaine.")

        assert fa._search_corpus(engine, "matte", ["159,20 euros"]) is True


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
        cat, reason = fa._classify_from_flags(False, self._flags(False, False, False), True, ["annexe 3", "159,20"], "montants DDI")
        assert cat == "missing_document"
        assert "corpus" in reason

    def test_unknown_corpus_scope_defers_to_llm(self):
        cat, hint = fa._classify_from_flags(None, self._flags(False, False, False), True, ["annexe 3", "159,20"], "montants DDI")
        assert cat is None
        assert "périmètre corpus" in hint

    def test_single_marker_not_in_corpus_defers_to_llm(self):
        # Un seul marqueur introuvable ne suffit pas à conclure missing_document
        cat, hint = fa._classify_from_flags(False, self._flags(False, False, False), True, ["annexe 3"], "")
        assert cat is None
        assert "missing_document" in hint

    def test_retrieval_issue_when_in_corpus_but_not_in_pool(self):
        cat, _ = fa._classify_from_flags(True, self._flags(False, False, False), True, ["m"], "")
        assert cat == "retrieval_issue"

    def test_incomplete_pool_defers_instead_of_false_retrieval_issue(self):
        cat, hint = fa._classify_from_flags(
            True,
            self._flags(False, False, False),
            True,
            ["m"],
            "",
            pool_complete=False,
        )
        assert cat is None
        assert "pool" in hint

    def test_candidate_cut_when_lost_between_pool_and_selector(self):
        cat, _ = fa._classify_from_flags(True, self._flags(True, False, False), True, ["m"], "")
        assert cat == "candidate_cut"

    def test_incomplete_selector_input_defers_candidate_cut(self):
        cat, hint = fa._classify_from_flags(
            True,
            self._flags(True, False, False),
            True,
            ["m"],
            "",
            selector_input_complete=False,
        )
        assert cat is None
        assert "selector" in hint

    def test_missing_generator_prompt_defers_stage_attribution(self):
        cat, hint = fa._classify_from_flags(
            True,
            self._flags(True, True, False),
            True,
            ["m"],
            "",
            generator_context_complete=False,
        )
        assert cat is None
        assert "prompt du générateur" in hint

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


# ---------------------------------------------------------------------------
# analyze_single – les erreurs techniques restent rejouables
# ---------------------------------------------------------------------------
class TestAnalyzeSingleFailureHandling:
    def test_unknown_corpus_scope_uses_llm_fallback(self, monkeypatch):
        captured_system_prompts = []
        monkeypatch.setattr(fa, "_extract_markers", lambda feedback: ("information absente", ["marqueur un", "marqueur deux"]))
        monkeypatch.setattr(
            fa,
            "_call_albert",
            lambda system_prompt, _user_prompt: captured_system_prompts.append(system_prompt) or ("generator_incomplete", "analyse de secours"),
        )
        monkeypatch.setattr(fa, "_save_analysis", lambda *args: True)

        category, reason = fa.analyze_single(
            {
                "id": 41,
                "question": "Q",
                "answer": "R",
                "selected_ministry": None,
                "v3_chunks_raw": None,
                "retrieved_chunks": None,
                "rag_prompt": None,
            },
            engine=object(),
        )

        assert category == "generator_incomplete"
        assert reason == "analyse de secours"
        assert "périmètre corpus" in captured_system_prompts[0]

    def test_llm_failure_is_not_persisted(self, monkeypatch):
        saved = []
        monkeypatch.setattr(fa, "_extract_markers", lambda feedback: ("", []))
        monkeypatch.setattr(fa, "_call_albert", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Albert indisponible")))
        monkeypatch.setattr(fa, "_save_analysis", lambda *args: saved.append(args) or True)

        with pytest.raises(RuntimeError, match="Albert indisponible"):
            fa.analyze_single({"id": 42, "question": "Q", "answer": "R"}, engine=object())

        assert saved == []

    def test_persistence_failure_is_reported(self, monkeypatch):
        monkeypatch.setattr(fa, "_extract_markers", lambda feedback: ("information absente", ["marqueur un", "marqueur deux"]))
        monkeypatch.setattr(fa, "_search_corpus", lambda *args: False)
        monkeypatch.setattr(fa, "_save_analysis", lambda *args: False)

        with pytest.raises(RuntimeError, match="échec de persistance"):
            fa.analyze_single({"id": 43, "question": "Q", "answer": "R"}, engine=object())
