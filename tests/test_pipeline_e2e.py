"""
End-to-end pipeline test — verifies the full RAG v3_clean flow.

Mocks DB and LLM calls so it runs without external dependencies.
Tests that a question goes through all pipeline stages and produces
a valid PipelineResult with answer, sources, context, and timing.

Usage:
    pytest tests/test_pipeline_e2e.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from assistant_rh_rag_pipeline.config import RAGConfig, SelectorConfig
from assistant_rh_rag_pipeline.models import (
    AggregatedSection,
    ContextItem,
    PipelineResult,
    RetrievedChunk,
)
from assistant_rh_rag_pipeline.query_processor import Intent, QueryProcessResult

# ---------------------------------------------------------------------------
# Fixtures: realistic fake data
# ---------------------------------------------------------------------------

def _make_chunk(idx: int, table: str = "matte", score: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk_{idx}",
        text=f"Le congé de mobilité permet aux agents de bénéficier d'un accompagnement personnalisé (chunk {idx}).",
        score=score - idx * 0.05,
        table_source=table,
        metadata={
            "document_id": f"doc_{idx // 3}",
            "heading": f"Section {idx // 2}",
            "publisher": "MATTE" if table == "matte" else "Service-Public",
            "section_id": f"sec_{idx // 2}",
        },
    )


def _make_section(idx: int, publisher: str = "MATTE") -> AggregatedSection:
    return AggregatedSection(
        section_id=f"sec_{idx}",
        heading=f"Fiche {idx} — Congé de mobilité",
        markdown=f"## Congé de mobilité\n\nL'agent peut bénéficier d'un congé... (section {idx})",
        chunks=[_make_chunk(idx * 2), _make_chunk(idx * 2 + 1)],
        score=0.85 - idx * 0.1,
        publisher=publisher,
        references_juridiques=[{"cid": f"LEGIARTI{idx}", "title": "CGFP", "number": f"L332-{idx}"}],
        metadata={"document_id": f"doc_{idx}"},
    )


def _make_context_item(idx: int, publisher: str = "MATTE") -> ContextItem:
    return ContextItem(
        section_id=f"sec_{idx}",
        heading=f"Fiche {idx} — Congé de mobilité",
        content=f"## Congé de mobilité\n\nContenu complet de la section {idx}...",
        score=0.85 - idx * 0.1,
        publisher=publisher,
        token_estimate=200,
        references_juridiques=[],
        metadata={"document_id": f"doc_{idx}"},
    )


FAKE_QUERY_RESULT = QueryProcessResult(
    original_query="Qu'est-ce que le congé de mobilité ?",
    processed_query="congé de mobilité",
    enriched_query="congé de mobilité",
    is_in_scope=True,
    intent=Intent.RAG_QUERY,
    needs_legal_search=False,
)


# ---------------------------------------------------------------------------
# Test: full pipeline run (non-streaming)
# ---------------------------------------------------------------------------

class TestPipelineE2E:
    """End-to-end test: question → query processing → retrieval → aggregation → selection → context build → generation → result."""

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_full_pipeline_produces_valid_result(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        # --- Arrange: wire up mocks ---
        mock_qp = MockQueryProcessor.return_value
        mock_qp.process.return_value = FAKE_QUERY_RESULT

        chunks = [_make_chunk(i) for i in range(6)]
        mock_retriever = MockRetriever.return_value
        mock_retriever.retrieve.return_value = chunks
        mock_retriever.config = MagicMock()
        mock_retriever.config.tables = ["matte", "service_public"]

        sections = [_make_section(0, "MATTE"), _make_section(1, "Service-Public")]
        mock_agg = MockAggregator.return_value
        mock_agg.aggregate.return_value = sections
        mock_agg.last_sections_before_rerank = 4
        mock_agg.last_sections_after_rerank = 2

        mock_sel = MockSelector.return_value
        mock_sel.enabled = True
        mock_sel.select.return_value = [sections[0]]
        mock_sel.all_rejected = False
        mock_sel.last_decisions = {"kept": [{"idx": 0, "heading": "Fiche 0"}], "removed": [{"idx": 1}]}
        mock_sel.last_reasoning = "Section 0 traite spécifiquement du congé de mobilité."
        mock_sel.last_raw_response = "{}"

        context_items = [_make_context_item(0)]
        mock_cb = MockContextBuilder.return_value
        mock_cb.build.return_value = context_items
        mock_cb.last_full_docs = ["doc_0"]
        mock_cb.last_legal_refs_found = 1
        mock_cb.last_legal_refs_total = 1

        mock_gen = MockGenerator.return_value
        mock_gen.generate.return_value = "Le congé de mobilité est un dispositif permettant à un agent public de..."
        mock_gen.last_full_prompt = "Contexte: ... Question: ..."
        mock_gen.last_system_prompt = "Vous êtes un assistant RH..."

        # --- Act ---
        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        from assistant_rh_rag_pipeline.pipeline import Pipeline
        pipe = Pipeline(config)
        result = pipe.run("Qu'est-ce que le congé de mobilité ?")

        # --- Assert ---
        assert isinstance(result, PipelineResult)
        assert result.query == "Qu'est-ce que le congé de mobilité ?"
        assert len(result.answer) > 20
        assert "congé de mobilité" in result.answer.lower() or "mobilité" in result.answer.lower()
        assert len(result.context_items) == 1
        assert result.context_items[0].publisher == "MATTE"

        # Verify pipeline called each stage in order
        mock_qp.process.assert_called_once()
        mock_retriever.retrieve.assert_called_once()
        mock_agg.aggregate.assert_called_once()
        mock_sel.select.assert_called_once()
        mock_cb.build.assert_called_once()
        mock_gen.generate.assert_called_once()

        # Timing should be populated
        assert isinstance(result.timing, dict)

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_out_of_scope_returns_direct_response(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """Non-RAG queries (chitchat, out of scope) should short-circuit."""
        oos_result = QueryProcessResult(
            original_query="Bonjour, comment vas-tu ?",
            processed_query="Bonjour, comment vas-tu ?",
            is_in_scope=False,
            intent=Intent.CHIT_CHAT,
            direct_response="Bonjour ! Je suis l'assistant RH. Comment puis-je vous aider ?",
        )
        MockQueryProcessor.return_value.process.return_value = oos_result

        config = RAGConfig()
        from assistant_rh_rag_pipeline.pipeline import Pipeline
        pipe = Pipeline(config)
        result = pipe.run("Bonjour, comment vas-tu ?")

        assert isinstance(result, PipelineResult)
        assert "assistant RH" in result.answer
        assert result.context_items == []
        # Retriever should NOT have been called
        MockRetriever.return_value.retrieve.assert_not_called()

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_selector_all_rejected_returns_no_answer(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """When the Selector rejects all sections, pipeline returns 'no answer' message."""
        MockQueryProcessor.return_value.process.return_value = FAKE_QUERY_RESULT

        MockRetriever.return_value.retrieve.return_value = [_make_chunk(0)]
        MockRetriever.return_value.config = MagicMock()
        MockRetriever.return_value.config.tables = ["matte"]

        MockAggregator.return_value.aggregate.return_value = [_make_section(0)]
        MockAggregator.return_value.last_sections_before_rerank = 1
        MockAggregator.return_value.last_sections_after_rerank = 1

        mock_sel = MockSelector.return_value
        mock_sel.enabled = True
        mock_sel.select.return_value = []  # all rejected
        mock_sel.all_rejected = True
        mock_sel.last_decisions = {}
        mock_sel.last_reasoning = "Aucune section pertinente."
        mock_sel.last_raw_response = "{}"

        MockContextBuilder.return_value.build.return_value = []
        MockContextBuilder.return_value.last_full_docs = []
        MockContextBuilder.return_value.last_legal_refs_found = 0
        MockContextBuilder.return_value.last_legal_refs_total = 0

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        from assistant_rh_rag_pipeline.pipeline import Pipeline
        pipe = Pipeline(config)
        result = pipe.run_with_trace("Quelle est la durée du congé spatial ?")

        assert isinstance(result, PipelineResult)
        assert "pas trouvé" in result.answer.lower() or "base de connaissances" in result.answer.lower()
        assert result.context_items == []
        assert result.metadata["selector_all_rejected"] is True

        diagnostics = result.metadata["rag_diagnostics"]
        assert diagnostics["query"]["original"] == "Quelle est la durée du congé spatial ?"
        assert diagnostics["query"]["enriched"] == "congé de mobilité"
        assert diagnostics["retrieval"]["tables_searched"] == ["matte"]
        assert diagnostics["retrieval"]["retrieved_chunks"][0]["chunk_id"] == "chunk_0"
        assert diagnostics["aggregation"]["aggregated_sections"][0]["section_id"] == "sec_0"
        assert diagnostics["reranker"]["section"]["enabled"] is True
        assert diagnostics["selector"]["all_rejected"] is True
        assert diagnostics["selector"]["rejection_reason"] == "Aucune section pertinente."

        stage_trace = result.metadata["stage_trace"]
        selector_output = stage_trace["stages"]["context-selector"]["output"]
        assert selector_output["selector_all_rejected"] is True
        assert selector_output["rejection_reason"] == "Aucune section pertinente."
        assert selector_output["selector_decision"] == "all_rejected"
        # Generator should NOT have been called
        MockGenerator.return_value.generate.assert_not_called()

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_run_with_trace_includes_stage_trace(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        MockQueryProcessor.return_value.process.return_value = FAKE_QUERY_RESULT

        MockRetriever.return_value.retrieve.return_value = [_make_chunk(0), _make_chunk(1)]
        MockRetriever.return_value.config = MagicMock()
        MockRetriever.return_value.config.tables = ["matte", "service_public"]

        MockAggregator.return_value.aggregate.return_value = [_make_section(0)]
        MockAggregator.return_value.last_sections_before_rerank = 1
        MockAggregator.return_value.last_sections_after_rerank = 1

        mock_sel = MockSelector.return_value
        mock_sel.enabled = True
        mock_sel.select.return_value = [_make_section(0)]
        mock_sel.all_rejected = False
        mock_sel.last_decisions = {"kept": [{"idx": 0}]}
        mock_sel.last_reasoning = "ok"
        mock_sel.last_raw_response = "{}"

        MockContextBuilder.return_value.build.return_value = [_make_context_item(0)]
        MockContextBuilder.return_value.last_full_docs = ["doc_0"]
        MockContextBuilder.return_value.last_legal_refs_found = 0
        MockContextBuilder.return_value.last_legal_refs_total = 0

        MockGenerator.return_value.generate.return_value = "Réponse test"

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        from assistant_rh_rag_pipeline.pipeline import Pipeline

        pipe = Pipeline(config)
        result = pipe.run_with_trace("Qu'est-ce que le congé de mobilité ?")

        stage_trace = result.metadata.get("stage_trace")
        assert isinstance(stage_trace, dict)
        assert stage_trace.get("schema_version") == "2026-05-05"

        stages = stage_trace.get("stages")
        assert isinstance(stages, dict)
        assert "query-processor" in stages
        assert "retriever" in stages
        assert "section-aggregator" in stages
        assert "context-selector" in stages
        assert "context-builder" in stages
        assert "generator" in stages

        qp_out = stages["query-processor"]["output"]
        assert qp_out["intent"] == "rag_query"

        generator_out = stages["generator"]["output"]
        assert generator_out["answer"] == "Réponse test"
