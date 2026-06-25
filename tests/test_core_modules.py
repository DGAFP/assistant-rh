"""
Tests for core RAG V3 Clean pipeline modules.

Tests are designed to run without a database connection (mocked where needed).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from assistant_rh_rag_pipeline.config import QueryProcessorConfig
from assistant_rh_rag_pipeline.models import (
    AggregatedSection,
    ContextItem,
    RetrievedChunk,
    estimate_tokens,
)
from assistant_rh_rag_pipeline.query_processor import Intent, QueryProcessor

# ---------------------------------------------------------------------------
# models.estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_french_text(self):
        assert estimate_tokens("Bonjour le monde") == 4  # 16 chars // 4

    def test_longer_text(self):
        text = "A" * 400
        assert estimate_tokens(text) == 100

    def test_aggregated_section_uses_estimate_tokens(self):
        sec = AggregatedSection(
            section_id="s1",
            heading="Test",
            markdown="A" * 200,
            chunks=[],
            score=0.9,
        )
        assert sec.token_estimate == 50


# ---------------------------------------------------------------------------
# context_selector._parse_response / _parse_reason / _extract_json
# ---------------------------------------------------------------------------


class TestSelectorParsing:
    def test_parse_json_with_selected_ids(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        raw = '```json\n{"selected_ids": [0, 2], "reason": "pertinent"}\n```'
        result = _parse_response(raw, n_items=5)
        assert result.ids == [0, 2]
        assert result.is_explicit_empty is False

    def test_parse_empty_selection_is_explicit_reject(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        raw = '{"selected_ids": [], "reason": "Aucun résultat pertinent"}'
        result = _parse_response(raw, n_items=3)
        assert result.is_explicit_empty is True

    def test_parse_with_selected_indices_key(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        raw = '{"selected_indices": [1, 3], "reason": "ok"}'
        result = _parse_response(raw, n_items=5)
        assert result.ids == [1, 3]

    def test_parse_filters_out_of_range_ids(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        raw = '{"selected_ids": [0, 10, -1, 2], "reason": "ok"}'
        result = _parse_response(raw, n_items=5)
        assert result.ids == [0, 2]

    def test_parse_garbage_returns_empty(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        result = _parse_response("not json at all", n_items=5)
        assert result.ids == []
        assert result.is_explicit_empty is False

    def test_parse_reason(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_reason

        raw = '{"selected_ids": [0], "reason": "La section traite du sujet"}'
        assert _parse_reason(raw) == "La section traite du sujet"

    def test_parse_reason_missing(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_reason

        raw = '{"selected_ids": [0]}'
        assert _parse_reason(raw) == ""

    def test_extract_json_with_code_block(self):
        from assistant_rh_rag_pipeline.context_selector import _extract_json

        raw = 'Voici ma réponse:\n```json\n{"key": "value"}\n```'
        assert _extract_json(raw) == {"key": "value"}

    def test_extract_json_plain(self):
        from assistant_rh_rag_pipeline.context_selector import _extract_json

        raw = '{"key": "value"}'
        assert _extract_json(raw) == {"key": "value"}

    def test_parse_string_ids_converted(self):
        from assistant_rh_rag_pipeline.context_selector import _parse_response

        raw = '{"selected_ids": ["0", "section 2"], "reason": "ok"}'
        result = _parse_response(raw, n_items=5)
        assert result.ids == [0, 2]


# ---------------------------------------------------------------------------
# db_helpers.load_prompt
# ---------------------------------------------------------------------------


class TestLoadPrompt:
    @patch("assistant_rh_rag_pipeline.db_helpers.get_prompt_content")
    def test_returns_primary_when_found(self, mock_get):
        from assistant_rh_rag_pipeline.db_helpers import load_prompt

        mock_get.side_effect = lambda name: "primary content" if name == "my-prompt.md" else None
        assert load_prompt("my-prompt.md", "fallback.md") == "primary content"

    @patch("assistant_rh_rag_pipeline.db_helpers.get_prompt_content")
    def test_returns_fallback_when_primary_missing(self, mock_get):
        from assistant_rh_rag_pipeline.db_helpers import load_prompt

        mock_get.side_effect = lambda name: "fallback content" if name == "fallback.md" else None
        assert load_prompt("missing.md", "fallback.md") == "fallback content"

    @patch("assistant_rh_rag_pipeline.db_helpers.get_prompt_content")
    def test_returns_default_when_both_missing(self, mock_get):
        from assistant_rh_rag_pipeline.db_helpers import load_prompt

        mock_get.return_value = None
        result = load_prompt("a.md", "b.md", default="default text")
        assert result == "default text"

    @patch("assistant_rh_rag_pipeline.db_helpers.get_prompt_content")
    def test_returns_none_when_all_missing(self, mock_get):
        from assistant_rh_rag_pipeline.db_helpers import load_prompt

        mock_get.return_value = None
        assert load_prompt("a.md", "b.md") is None


# ---------------------------------------------------------------------------
# query_processor — legal-search guardrails
# ---------------------------------------------------------------------------


def _make_classify_return(theme: str = "remuneration", needs_legal: bool = False) -> dict:
    """Mock for QueryProcessor._classify success path."""
    return {
        "intent": Intent.RAG_QUERY,
        "confidence": 0.95,
        "reasoning": "Question RH",
        "needs_legal": needs_legal,
        "theme": theme,
        "enriched_query": "",
        "query_for_retrieval": None,
        "direct_response": None,
        "raw": "{}",
        "classify_ok": True,
    }


@pytest.mark.parametrize(
    "query, expected_needs_legal, label",
    [
        # Original singular forms (regression coverage)
        (
            "Dans quels cas l'administration est-elle subrogée aux indemnités journalières dues à un agent contractuel ?",
            True,
            "subrogation+indemnites+agent_contractuel",
        ),
        (
            "Dans quel délai l'administration doit-elle notifier à l'agent son intention de renouveler ou non un contrat de projet ?",
            True,
            "contrat_projet+rule",
        ),
        (
            "Quelles vérifications administratives peuvent empêcher le recrutement "
            "d'un agent contractuel, notamment sur le casier judiciaire, la "
            "situation au regard du service national ou le droit au séjour ?",
            True,
            "verifications+casier+sejour",
        ),
        (
            "Quelle vérification administrative peut empêcher le recrutement d'un agent contractuel au regard du droit au séjour ?",
            True,
            "singular_rule_phrase",
        ),
        (
            "Sous quelles conditions l'agent est-il réemployé sur son précédent emploi au terme d'un congé parental ?",
            True,
            "conditions+conge_parental",
        ),
        # ── French plural morphology (review finding #1) ─────────────────────
        (
            "Sous quelles conditions les agents contractuels sont-ils réemployés en congés parentaux ?",
            True,
            "plural_agents+conges_parentaux",
        ),
        (
            "Quels délais s'appliquent au renouvellement des contrats de projet ?",
            True,
            "plural_contrats_de_projet",
        ),
        (
            "À partir de quand les emplois permanents peuvent-ils être confiés à des agents contractuels ?",
            True,
            "plural_emplois_permanents",
        ),
        # ── Canonical Légifrance citation (review finding #2) ────────────────
        (
            "Que dit l'article L. 132-1 sur le congé de maladie ?",
            True,
            "article_L_dot_space",
        ),
        (
            "Pour les articles R. 7-2 et suivants, quelle est la procédure ?",
            True,
            "articles_plural_dot_space",
        ),
        # ── Unaccented French typing (review finding #9) ─────────────────────
        (
            "A partir de quand peut-on recruter un agent contractuel sur un emploi permanent ?",
            True,
            "ascii_a_partir_de_quand",
        ),
        (
            "Quelles verifications administratives peuvent empecher le recrutement d'un agent contractuel au regard du droit au sejour ?",
            True,
            "ascii_verifications",
        ),
        # ── NFD-decomposed input (review finding #10) ────────────────────────
        (
            # Same text as the first case, but NFD-decomposed (Mac clipboard).
            __import__("unicodedata").normalize(
                "NFD",
                "Dans quels cas l'administration est-elle subrogée aux indemnités journalières dues à un agent contractuel ?",
            ),
            True,
            "nfd_subrogation",
        ),
        # ── Negative cases ───────────────────────────────────────────────────
        (
            "Comment demander des congés annuels à son manager ?",
            False,
            "generic_conges_question",
        ),
        # Bare `loi` no longer triggers (review finding #8: false positives).
        (
            "Est-ce que la loi du plus fort règne dans ce service ?",
            False,
            "loi_idiom_no_qualifier",
        ),
        # Qualified `loi du <year>` still triggers.
        (
            "La loi du 26 janvier 1984 prévoit-elle des congés parentaux ?",
            True,
            "loi_du_year_qualifier",
        ),
        # Review v2 finding: `loi n[°o]` over-matched `loi nouvelle/normale`
        # because `n[°o]` consumed the bare letter `o` followed by anything.
        (
            "Quelle loi nouvelle s'applique ?",
            False,
            "loi_nouvelle_no_false_positive",
        ),
        (
            "La loi normale prévoit-elle des exceptions ?",
            False,
            "loi_normale_no_false_positive",
        ),
        # Qualified `loi n° <digit>` must still trigger.
        (
            "Selon la loi n° 84-16, quelles règles s'appliquent ?",
            True,
            "loi_numero_qualifier",
        ),
        # Review v2 finding: article regex over-fired on `article a 5 ans`
        # (`[a-z]?` consumed the lone preposition `a`).
        (
            "Cet article a 5 ans, faut-il le relire ?",
            False,
            "article_a_5_ans_no_false_positive",
        ),
        (
            "L'article du blog a 3 jours seulement.",
            False,
            "article_du_blog_no_false_positive",
        ),
        # Review v2 finding: `arrête` (verb imperative) folds to `arrete`
        # same as the noun `arrêté` (decree) — must not match without a
        # qualifying context.
        (
            "Arrête de m'embêter avec ces questions !",
            False,
            "arrete_verb_no_false_positive",
        ),
        (
            "Il arrête de venir au bureau.",
            False,
            "arrete_indicative_no_false_positive",
        ),
        # Qualified arrêté noun must still trigger.
        (
            "Selon l'arrêté ministériel du 17 janvier 1986, quels droits ?",
            True,
            "arrete_ministeriel_qualified",
        ),
        # Review v2 finding: Unicode dashes (U+2010..U+2014) used by Word/PDF
        # paste must be normalized in _fold() so canonical citations match.
        (
            "Selon l'article L‑132‑1 du CGFP, quels droits ?",  # U+2011 non-breaking hyphen
            True,
            "article_unicode_nbhyphen",
        ),
        (
            "L'article L–132 fixe les modalités.",  # U+2013 en dash
            True,
            "article_unicode_endash",
        ),
        # Review v3 finding #2: space-separated citation form (letter + space +
        # digit, no dot/dash) must match — it regressed in v2.
        (
            "Que dit l'article L 132-1 du CGFP ?",
            True,
            "article_letter_space_digit",
        ),
        (
            "Selon l'article L 5212-1, quelles obligations ?",
            True,
            "article_letter_space_long",
        ),
        # …but the verb/preposition "a" between "article" and a number must NOT
        # (the spaced form is restricted to code letters [lrd]).
        (
            "Cet article a 5 ans, faut-il le relire ?",
            False,
            "article_a_space_digit_still_rejected",
        ),
        # Review v3 finding #6: a bare 4-digit run is a year, not an article.
        (
            "Mon article 2025 du blog parle de RH.",
            False,
            "article_year_no_false_positive",
        ),
        (
            "L'article 1958 de la constitution.",
            False,
            "article_year_1958_no_false_positive",
        ),
        # Bare short article number and hyphenated sub-article still match.
        (
            "Que prévoit l'article 3-2 pour les contractuels ?",
            True,
            "article_hyphenated_subarticle",
        ),
        # Review v3 finding #5: bare-noun arrêté preceded by a determiner must
        # trigger (it regressed to False in v2).
        (
            "Quel arrêté fixe la prime de service des agents contractuels ?",
            True,
            "arrete_determiner_quel",
        ),
        (
            "Un arrêté encadre-t-il les congés des contractuels ?",
            True,
            "arrete_determiner_un",
        ),
        # …but the imperative/indicative verb "arrête" must still NOT trigger.
        (
            "Arrête de poser des questions, merci.",
            False,
            "arrete_imperative_still_rejected",
        ),
        (
            "Il les arrête à la frontière.",
            False,
            "arrete_object_pronoun_still_rejected",
        ),
    ],
)
@patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
@patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
def test_legal_search_heuristic_matrix(mock_classify, _mock_acronyms, query, expected_needs_legal, label):
    """Single matrix covering plural/accent/canonical-citation/loi-FP/regression cases."""
    mock_classify.return_value = _make_classify_return()
    proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))
    result = proc.process(query)
    assert result.intent == Intent.RAG_QUERY
    assert result.needs_legal_search is expected_needs_legal, f"[{label}] expected needs_legal_search={expected_needs_legal} for: {query!r}"
    # LLM signal is preserved separately so observability can distinguish
    # LLM-driven from heuristic-driven decisions.
    assert result.needs_legal_search_llm is False


class TestLegalSearchGating:
    """Heuristic must be gated on classify success AND enable_intent_gating."""

    @patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
    @patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
    def test_heuristic_skipped_on_classify_failure(self, mock_classify, _mock_acronyms):
        # Simulate the exception fallback (no classify_ok flag, no needs_legal key).
        mock_classify.return_value = {
            "intent": Intent.RAG_QUERY,
            "confidence": 0.5,
            "reasoning": "LLM outage",
            "classify_ok": False,
        }
        proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))
        result = proc.process("Dans quels cas l'administration est-elle subrogée aux indemnités journalières dues à un agent contractuel ?")
        # Pre-PR safe default of False is preserved on LLM outage.
        assert result.needs_legal_search is False
        assert result.needs_legal_search_llm is None

    @patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
    def test_heuristic_skipped_when_intent_gating_disabled(self, _mock_acronyms):
        proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=False))
        result = proc.process("Dans quels cas l'administration est-elle subrogée aux indemnités journalières dues à un agent contractuel ?")
        # Heuristic does not run; fallback path keeps needs_legal_search=False.
        assert result.needs_legal_search is False
        assert result.needs_legal_search_llm is None

    @patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
    @patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
    def test_llm_true_is_always_preserved(self, mock_classify, _mock_acronyms):
        mock_classify.return_value = _make_classify_return(needs_legal=True)
        proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))
        result = proc.process("Comment demander des congés annuels ?")
        assert result.needs_legal_search is True
        assert result.needs_legal_search_llm is True

    @patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
    @patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
    def test_observability_preserves_llm_false_under_heuristic_override(self, mock_classify, _mock_acronyms):
        mock_classify.return_value = _make_classify_return(needs_legal=False)
        proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))
        result = proc.process("Dans quel délai l'administration doit-elle notifier le renouvellement d'un contrat de projet ?")
        # Effective decision is heuristic True, but the LLM value (False) is preserved.
        assert result.needs_legal_search is True
        assert result.needs_legal_search_llm is False


class TestQueryProcessorNFCNormalization:
    """NFC-normalize at process() entry so the LLM, retriever, and replay cache
    all see the same byte sequence regardless of client clipboard form."""

    @patch("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", return_value={})
    @patch("assistant_rh_rag_pipeline.query_processor.QueryProcessor._classify")
    def test_nfd_and_nfc_inputs_normalize_to_same_processed_query(self, mock_classify, _mock_acronyms):
        import unicodedata as _u

        mock_classify.return_value = _make_classify_return()
        proc = QueryProcessor(QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=True))

        nfc_query = "Quelles vérifications administratives ?"
        nfd_query = _u.normalize("NFD", nfc_query)
        assert nfc_query != nfd_query, "NFC and NFD inputs must differ for the test to be meaningful"

        result_nfc = proc.process(nfc_query)
        result_nfd = proc.process(nfd_query)

        # Both inputs round-trip to the same NFC string.
        assert _u.is_normalized("NFC", result_nfc.original_query)
        assert _u.is_normalized("NFC", result_nfd.original_query)
        assert result_nfc.original_query == result_nfd.original_query


# ---------------------------------------------------------------------------
# section_aggregator — grouping & scoring
# ---------------------------------------------------------------------------


class TestSectionAggregator:
    def _make_chunk(self, chunk_id, section_id=None, score=0.9, table="MATTE"):
        return RetrievedChunk(
            chunk_id=chunk_id,
            text=f"Text of {chunk_id}",
            score=score,
            table_source=table,
            metadata={"source_name": "Test Doc"},
            section_id=section_id,
        )

    @patch("assistant_rh_rag_pipeline.section_aggregator.SectionAggregator._fetch_sections")
    def test_standalone_chunks_grouped_separately(self, mock_fetch):
        from assistant_rh_rag_pipeline.config import SectionAggregationConfig
        from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

        mock_fetch.return_value = {}
        agg = SectionAggregator(SectionAggregationConfig(), dsn="unused")

        chunks = [
            self._make_chunk("c1", section_id=None, score=0.9),
            self._make_chunk("c2", section_id=None, score=0.8),
        ]
        sections = agg.aggregate(chunks)
        assert len(sections) == 2
        for sec in sections:
            assert sec.section_id is None

    @patch("assistant_rh_rag_pipeline.section_aggregator.SectionAggregator._fetch_sections")
    def test_same_section_chunks_grouped(self, mock_fetch):
        from assistant_rh_rag_pipeline.config import SectionAggregationConfig
        from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

        mock_fetch.return_value = {
            "sec-1": {
                "heading": "Section 1",
                "section_markdown": "Full section text",
                "doc_id": "doc-1",
                "doc_title": "Doc Title",
                "doc_url": None,
                "doc_token_count": 500,
                "doc_publisher": "MATTE",
                "doc_date": "2025-01-01",
                "references_juridiques": None,
                "heading_path": "/root/sec1",
            },
        }
        agg = SectionAggregator(SectionAggregationConfig(), dsn="unused")

        chunks = [
            self._make_chunk("c1", section_id="sec-1", score=0.95),
            self._make_chunk("c2", section_id="sec-1", score=0.85),
            self._make_chunk("c3", section_id=None, score=0.7),
        ]
        sections = agg.aggregate(chunks)
        assert len(sections) == 2

        grouped = [s for s in sections if s.section_id == "sec-1"][0]
        assert len(grouped.chunks) == 2
        assert grouped.heading == "Section 1"

    @patch("assistant_rh_rag_pipeline.section_aggregator.SectionAggregator._fetch_sections")
    def test_scoring_uses_weights(self, mock_fetch):
        from assistant_rh_rag_pipeline.config import SectionAggregationConfig
        from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

        mock_fetch.return_value = {}
        config = SectionAggregationConfig(
            weight_max_score=1.0,
            weight_mean_score=0.0,
            weight_chunk_count=0.0,
        )
        agg = SectionAggregator(config, dsn="unused")

        chunks = [self._make_chunk("c1", score=0.5)]
        sections = agg.aggregate(chunks)
        assert abs(sections[0].score - 0.5) < 0.01

    @patch("assistant_rh_rag_pipeline.section_aggregator.SectionAggregator._fetch_sections")
    def test_standalone_service_public_chunk_uses_canonical_document_metadata(self, mock_fetch):
        from assistant_rh_rag_pipeline.config import SectionAggregationConfig
        from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

        mock_fetch.return_value = {}
        agg = SectionAggregator(SectionAggregationConfig(), dsn="unused")
        chunk = RetrievedChunk(
            chunk_id="c-f527",
            text="Chunk text",
            score=0.8,
            table_source="Service-Public",
            metadata={
                "source_name": "F527.xml",
                "doc_short_id": "F527",
                "doc_title": "Remboursement des frais de déplacement dans la fonction publique",
                "doc_url": "https://www.service-public.gouv.fr/particuliers/vosdroits/F527",
            },
            section_id=None,
        )

        sections = agg.aggregate([chunk])

        assert len(sections) == 1
        assert sections[0].heading == "Remboursement des frais de déplacement dans la fonction publique"
        assert sections[0].metadata["doc_short_id"] == "F527"
        assert sections[0].metadata["doc_title"] == "Remboursement des frais de déplacement dans la fonction publique"
        assert sections[0].metadata["doc_url"] == "https://www.service-public.gouv.fr/particuliers/vosdroits/F527"

    @patch("assistant_rh_rag_pipeline.section_aggregator.SectionAggregator._fetch_sections")
    def test_standalone_dgafp_chunk_promotes_legal_metadata(self, mock_fetch):
        from assistant_rh_rag_pipeline.config import SectionAggregationConfig
        from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

        mock_fetch.return_value = {}
        agg = SectionAggregator(SectionAggregationConfig(), dsn="unused")
        chunk = RetrievedChunk(
            chunk_id="LEGIARTI000045662634_0",
            text="Article 1-3 text",
            score=0.99,
            table_source="DGAFP",
            metadata={
                "cid": "LEGIARTI000045662634",
                "full_title": ("Décret n° 86-83 du 17 janvier 1986 relatif aux dispositions générales applicables aux agents contractuels de l'Etat"),
                "title": "Décret n° 86-83 du 17 janvier 1986",
                "number": "Article 1-3",
                "url": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000045662634",
            },
            section_id=None,
        )

        sections = agg.aggregate([chunk])

        assert len(sections) == 1
        assert sections[0].section_id is None
        assert sections[0].document_id is None
        assert (
            sections[0].heading
            == "Décret n° 86-83 du 17 janvier 1986 relatif aux dispositions générales applicables aux agents contractuels de l'Etat"
        )
        assert sections[0].metadata["doc_id"] == "LEGIARTI000045662634"
        assert sections[0].metadata["doc_short_id"] == "LEGIARTI000045662634"
        assert (
            sections[0].metadata["doc_title"]
            == "Décret n° 86-83 du 17 janvier 1986 relatif aux dispositions générales applicables aux agents contractuels de l'Etat"
        )
        assert sections[0].metadata["cid"] == "LEGIARTI000045662634"
        assert sections[0].metadata["number"] == "Article 1-3"


# ---------------------------------------------------------------------------
# context_builder — ref collection
# ---------------------------------------------------------------------------


class TestContextBuilderRefs:
    def test_collect_ref_numbers_from_json_string(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1",
            heading="Test",
            content="c",
            score=0.9,
            references_juridiques=json.dumps(
                [
                    {"number": "L332-2", "title": "CGFP"},
                    {"number": "L332-6", "title": "CGFP"},
                ]
            ),
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert set(numbers) == {"L332-2", "L332-6"}

    def test_collect_ref_numbers_from_list(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1",
            heading="Test",
            content="c",
            score=0.9,
            references_juridiques=[{"number": "R123-4"}],
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert numbers == ["R123-4"]

    def test_collect_ref_numbers_skips_invalid_json(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1",
            heading="Test",
            content="c",
            score=0.9,
            references_juridiques="not valid json",
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert numbers == []

    def test_collect_ref_numbers_empty_refs(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1",
            heading="Test",
            content="c",
            score=0.9,
            references_juridiques=None,
        )
        assert ContextBuilder._collect_ref_numbers([item]) == []


class TestContextBuilderBuild:
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._load_full_document")
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._resolve_cids", return_value={})
    def test_prefers_higher_scoring_standalone_before_doc_entire(self, _mock_refs, mock_load_doc):
        from assistant_rh_rag_pipeline.config import ContextBuildConfig
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        mock_load_doc.return_value = {
            "doc_id": "doc-sp",
            "title": "Document Service-Public",
            "source_url": "https://example.test/service-public",
            "publisher": "Service-Public",
            "doc_markdown": "Contenu complet service-public",
            "token_count": 200,
        }

        builder = ContextBuilder(ContextBuildConfig(max_full_docs=1, doc_entire_threshold=500, max_sections=5), dsn="unused")
        sections = [
            AggregatedSection(
                section_id=None,
                heading="",
                markdown="Réponse DGAFP prioritaire",
                chunks=[],
                score=0.9,
                publisher="DGAFP",
            ),
            AggregatedSection(
                section_id="sec-sp",
                heading="Question annexe",
                markdown="Extrait Service-Public",
                chunks=[],
                score=0.2,
                document_id="doc-sp",
                publisher="Service-Public",
                metadata={"doc_token_count": 200, "doc_title": "Document Service-Public", "doc_url": "https://example.test/service-public"},
            ),
        ]

        result = builder.build(sections)

        # Highest-scoring item wins position 0, regardless of whether it's
        # standalone or doc-entire. A lower-score doc-entire may follow, but
        # it must not preempt the standalone.
        assert result[0].publisher == "DGAFP"
        assert result[0].content == "Réponse DGAFP prioritaire"
        doc_entire_idx = next(
            (i for i, item in enumerate(result) if item.publisher == "Service-Public" and item.metadata.get("is_doc_entire")),
            None,
        )
        if doc_entire_idx is not None:
            assert doc_entire_idx > 0, "lower-score doc-entire preempted the standalone"

        # Uniqueness: a standalone with empty section_id/heading must not appear twice.
        dgafp_count = sum(1 for item in result if item.publisher == "DGAFP")
        assert dgafp_count == 1, f"DGAFP item duplicated: {dgafp_count} copies"

    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._load_full_document")
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._resolve_cids", return_value={})
    def test_keeps_doc_entire_when_document_beats_standalone(self, _mock_refs, mock_load_doc):
        from assistant_rh_rag_pipeline.config import ContextBuildConfig
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        mock_load_doc.return_value = {
            "doc_id": "doc-sp",
            "title": "Document Service-Public",
            "source_url": "https://example.test/service-public",
            "publisher": "Service-Public",
            "doc_markdown": "Contenu complet service-public",
            "token_count": 200,
        }

        builder = ContextBuilder(ContextBuildConfig(max_full_docs=1, doc_entire_threshold=500, max_sections=5), dsn="unused")
        sections = [
            AggregatedSection(
                section_id="sec-sp",
                heading="Question annexe",
                markdown="Extrait Service-Public",
                chunks=[],
                score=0.9,
                document_id="doc-sp",
                publisher="Service-Public",
                metadata={"doc_token_count": 200, "doc_title": "Document Service-Public", "doc_url": "https://example.test/service-public"},
            ),
            AggregatedSection(
                section_id=None,
                heading="",
                markdown="Réponse DGAFP secondaire",
                chunks=[],
                score=0.2,
                publisher="DGAFP",
            ),
        ]

        result = builder.build(sections)

        assert result[0].publisher == "Service-Public"
        assert result[0].metadata.get("is_doc_entire") is True

    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._load_full_document")
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._resolve_cids", return_value={})
    def test_multiple_small_docs_all_included(self, _mock_refs, mock_load_doc):
        """Review finding #5: snapshot best_standalone_score should not suppress
        sibling small docs whose best chunk falls below the standalone."""
        from assistant_rh_rag_pipeline.config import ContextBuildConfig
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        def _doc(doc_id):
            return {
                "doc_id": doc_id,
                "title": f"Doc {doc_id}",
                "source_url": f"https://example.test/{doc_id}",
                "publisher": "Service-Public",
                "doc_markdown": f"Contenu {doc_id}",
                "token_count": 200,
            }

        mock_load_doc.side_effect = lambda doc_id: _doc(doc_id)
        builder = ContextBuilder(
            ContextBuildConfig(max_full_docs=3, doc_entire_threshold=500, max_sections=10),
            dsn="unused",
        )
        sections = [
            AggregatedSection(
                section_id=None,
                heading="",
                markdown="DGAFP",
                chunks=[],
                score=0.90,
                publisher="DGAFP",
            ),
            AggregatedSection(
                section_id="sec-a",
                heading="A",
                markdown="A",
                chunks=[],
                score=0.91,
                document_id="doc-a",
                publisher="Service-Public",
                metadata={"doc_token_count": 200},
            ),
            AggregatedSection(
                section_id="sec-b",
                heading="B",
                markdown="B",
                chunks=[],
                score=0.88,
                document_id="doc-b",
                publisher="Service-Public",
                metadata={"doc_token_count": 200},
            ),
            AggregatedSection(
                section_id="sec-c",
                heading="C",
                markdown="C",
                chunks=[],
                score=0.87,
                document_id="doc-c",
                publisher="Service-Public",
                metadata={"doc_token_count": 200},
            ),
        ]

        result = builder.build(sections)
        doc_entire_ids = {item.metadata.get("doc_id") for item in result if item.metadata.get("is_doc_entire")}
        # All three small docs must surface as doc-entire (up to max_full_docs).
        # Pre-fix code suppressed doc-b (0.88) and doc-c (0.87) because they
        # both fell below the frozen best_standalone_score (0.90).
        assert doc_entire_ids == {"doc-a", "doc-b", "doc-c"}, doc_entire_ids
        # Final ordering reflects score: doc-a (0.91) > DGAFP (0.90) > doc-b (0.88) > doc-c (0.87).
        publishers_in_order = [item.publisher for item in result if item in result[:4]]
        assert publishers_in_order[0] == "Service-Public"  # doc-a 0.91
        assert publishers_in_order[1] == "DGAFP"  # 0.90

    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._load_full_document")
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._resolve_cids", return_value={})
    def test_tied_scores_do_not_let_doc_entire_preempt_standalone(self, _mock_refs, mock_load_doc):
        """Review finding #7: strict `<` defeated promotion intent on ties."""
        from assistant_rh_rag_pipeline.config import ContextBuildConfig
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        mock_load_doc.return_value = {
            "doc_id": "doc-sp",
            "title": "Document Service-Public",
            "source_url": "https://example.test/service-public",
            "publisher": "Service-Public",
            "doc_markdown": "Contenu",
            "token_count": 200,
        }

        builder = ContextBuilder(
            ContextBuildConfig(max_full_docs=1, doc_entire_threshold=500, max_sections=5),
            dsn="unused",
        )
        sections = [
            AggregatedSection(
                section_id=None,
                heading="",
                markdown="DGAFP",
                chunks=[],
                score=0.9,
                publisher="DGAFP",
            ),
            AggregatedSection(
                section_id="sec-sp",
                heading="A",
                markdown="A",
                chunks=[],
                score=0.9,
                document_id="doc-sp",
                publisher="Service-Public",
                metadata={"doc_token_count": 200},
            ),
        ]
        result = builder.build(sections)
        # Stable sort keeps retrieval order on tie; DGAFP arrived first in
        # the input list, so it stays at position 0.
        assert result[0].publisher == "DGAFP"

    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._load_full_document", return_value=None)
    @patch("assistant_rh_rag_pipeline.context_builder.ContextBuilder._resolve_cids", return_value={})
    def test_triangulation_does_not_starve_primary_refs_budget(self, _mock_refs, _mock_load):
        """Review v3 finding #4: moving the sort after triangulation let a
        higher-scored triangulation (diversity) item consume the legal-refs
        budget ahead of a lower-scored primary-content item. The refs pass must
        allocate primary-content-first."""
        from assistant_rh_rag_pipeline.config import ContextBuildConfig
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder
        from assistant_rh_rag_pipeline.models import estimate_tokens

        refs_primary = [{"number": "L-PRIMARY"}]
        refs_tri = [{"number": "L-TRIANG"}]
        one_block = estimate_tokens(ContextBuilder._format_references(refs_primary))

        # Budget fits exactly one refs block.
        builder = ContextBuilder(
            ContextBuildConfig(max_sections=2, doc_entire_threshold=0, legal_refs_budget=one_block),
            dsn="unused",
        )
        sections = [
            # Highest score → sets primary_publisher = DGAFP; carries no refs.
            AggregatedSection(
                section_id="a",
                heading="A",
                markdown="A",
                chunks=[],
                score=0.9,
                publisher="DGAFP",
            ),
            # Primary-content item, low score, HAS refs. Survives Step 2.
            AggregatedSection(
                section_id="b",
                heading="B",
                markdown="B",
                chunks=[],
                score=0.5,
                publisher="DGAFP",
                references_juridiques=refs_primary,
            ),
            # Triangulation candidate: different publisher, high score, HAS refs.
            # Excluded from Step 2 by max_sections, force-added by triangulation.
            AggregatedSection(
                section_id="c",
                heading="C",
                markdown="C",
                chunks=[],
                score=0.85,
                publisher="MATTE",
                references_juridiques=refs_tri,
            ),
        ]

        result = builder.build(sections)
        by_id = {it.section_id: it for it in result}
        # Triangulation item C was indeed added and flagged.
        assert by_id["c"].metadata.get("is_triangulation") is True
        # The primary-content item B got its refs block; the triangulation item
        # C did not (budget fit only one, and primary wins).
        assert "References juridiques" in by_id["b"].content
        assert "References juridiques" not in by_id["c"].content


# ---------------------------------------------------------------------------
# context_builder — format_for_prompt (static)
# ---------------------------------------------------------------------------


class TestContextBuilderFormat:
    def test_format_includes_heading_and_content(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        items = [
            ContextItem(
                section_id="s1",
                heading="Mon Titre",
                content="Contenu de la section",
                score=0.9,
                publisher="MATTE",
            ),
        ]
        result = ContextBuilder.format_for_prompt(items)
        assert "Mon Titre" in result
        assert "Contenu de la section" in result

    def test_format_empty_items(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        result = ContextBuilder.format_for_prompt([])
        assert result == "" or "Aucun" in result or len(result) < 50
