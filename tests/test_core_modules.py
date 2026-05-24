"""
Tests for core RAG V3 Clean pipeline modules.

Tests are designed to run without a database connection (mocked where needed).
"""
from __future__ import annotations

import json
from unittest.mock import patch

from assistant_rh_rag_pipeline.models import (
    AggregatedSection,
    ContextItem,
    RetrievedChunk,
    estimate_tokens,
)

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


# ---------------------------------------------------------------------------
# context_builder — ref collection
# ---------------------------------------------------------------------------

class TestContextBuilderRefs:
    def test_collect_ref_numbers_from_json_string(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1", heading="Test", content="c", score=0.9,
            references_juridiques=json.dumps([
                {"number": "L332-2", "title": "CGFP"},
                {"number": "L332-6", "title": "CGFP"},
            ]),
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert set(numbers) == {"L332-2", "L332-6"}

    def test_collect_ref_numbers_from_list(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1", heading="Test", content="c", score=0.9,
            references_juridiques=[{"number": "R123-4"}],
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert numbers == ["R123-4"]

    def test_collect_ref_numbers_skips_invalid_json(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1", heading="Test", content="c", score=0.9,
            references_juridiques="not valid json",
        )
        numbers = ContextBuilder._collect_ref_numbers([item])
        assert numbers == []

    def test_collect_ref_numbers_empty_refs(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        item = ContextItem(
            section_id="s1", heading="Test", content="c", score=0.9,
            references_juridiques=None,
        )
        assert ContextBuilder._collect_ref_numbers([item]) == []


# ---------------------------------------------------------------------------
# context_builder — format_for_prompt (static)
# ---------------------------------------------------------------------------

class TestContextBuilderFormat:
    def test_format_includes_heading_and_content(self):
        from assistant_rh_rag_pipeline.context_builder import ContextBuilder

        items = [
            ContextItem(
                section_id="s1", heading="Mon Titre",
                content="Contenu de la section", score=0.9,
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
