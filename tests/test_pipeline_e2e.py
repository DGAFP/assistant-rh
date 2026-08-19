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

from assistant_rh_rag_pipeline.config import RAGConfig, SearchMode, SelectorConfig
from assistant_rh_rag_pipeline.models import (
    CHUNK_LOG_KEYS,
    AggregatedSection,
    ContextItem,
    PipelineResult,
    RetrievedChunk,
    serialize_section_chunks,
)
from assistant_rh_rag_pipeline.query_processor import Intent, QueryProcessResult
from assistant_rh_rag_pipeline.section_aggregator import (
    SectionAggregationDiagnostics,
    SectionAggregationResult,
)

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


def _aggregation_result(
    sections: list[AggregatedSection],
    *,
    before: int | None = None,
    after: int | None = None,
    reranker_status: str = "completed",
) -> SectionAggregationResult:
    return SectionAggregationResult(
        sections=sections,
        diagnostics=SectionAggregationDiagnostics(
            sections_before_rerank=len(sections) if before is None else before,
            sections_after_rerank=len(sections) if after is None else after,
            reranker_status=reranker_status,
            chunks_before_rerank=serialize_section_chunks(sections),
            chunks_after_rerank=serialize_section_chunks(sections, include_rerank_score=reranker_status == "completed"),
        ),
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


def test_intent_value_handles_missing_intent() -> None:
    from assistant_rh_rag_pipeline.pipeline import _intent_value

    qr = QueryProcessResult(original_query="q", processed_query="q")
    assert _intent_value(qr) == "rag_query"

    qr.intent = None
    assert _intent_value(qr) == "unknown"


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
        mock_agg.aggregate_with_diagnostics.return_value = _aggregation_result(
            sections,
            before=4,
            after=2,
        )

        mock_sel = MockSelector.return_value
        mock_sel.enabled = True
        mock_sel.select.return_value = [sections[0]]
        mock_sel.all_rejected = False
        mock_sel.last_decisions = {"kept": [{"idx": 0, "heading": "Fiche 0"}], "removed": [{"idx": 1}]}
        mock_sel.last_reasoning = "Section 0 traite spécifiquement du congé de mobilité."
        mock_sel.last_raw_response = "{}"
        mock_sel.last_prompt_chars = 321

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
        mock_agg.aggregate_with_diagnostics.assert_called_once()
        mock_sel.select.assert_called_once_with(
            "congé de mobilité",
            sections,
            ministry=None,
        )
        mock_cb.build.assert_called_once()
        mock_gen.generate.assert_called_once()

        # Timing should be populated
        assert isinstance(result.timing, dict)

        # Chunk-level traces are carried through v3_metadata for chat_runs logging.
        meta = result.metadata
        assert len(meta["chunks_raw"]) == 6
        raw_entry = meta["chunks_raw"][0]
        assert tuple(raw_entry.keys()) == CHUNK_LOG_KEYS
        assert raw_entry["doc_id"] == "doc_0"
        assert raw_entry["doc_publisher"] == "MATTE"
        assert raw_entry["section_heading"] == "Section 0"
        assert raw_entry["rerank_score"] is None
        assert meta["chunks_before_rerank"][0]["rerank_score"] is None
        assert meta["chunks_after_rerank"][0]["rerank_score"] is not None
        assert meta["context_before_selector"][0]["rerank_score"] is not None
        assert meta["retrieval_attempts"][0]["chunks_raw"] == meta["chunks_raw"]
        assert meta["retrieval_attempts"][0]["context_items_ref"][0]["doc_id"] == "doc_0"
        assert meta["selector_prompt_chars"] == 321
        assert meta["selector_response_chars"] == 2

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

        MockAggregator.return_value.aggregate_with_diagnostics.return_value = _aggregation_result(
            [_make_section(0)],
            before=1,
            after=1,
        )

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
        assert result.metadata["selector_retry_triggered"] is True
        assert result.metadata["selector_retry_succeeded"] is False
        assert len(result.metadata["retrieval_attempts"]) == 2
        assert result.metadata["retrieval_attempts"][0]["name"] == "initial"
        assert result.metadata["retrieval_attempts"][1]["name"] == "selector_retry"

        diagnostics = result.metadata["rag_diagnostics"]
        assert diagnostics["query"]["original"] == "Quelle est la durée du congé spatial ?"
        assert diagnostics["query"]["enriched"] == "congé de mobilité"
        assert diagnostics["retrieval"]["tables_searched"] == ["matte"]
        assert diagnostics["retrieval"]["retrieved_chunks"][0]["chunk_id"] == "chunk_0"
        assert diagnostics["aggregation"]["aggregated_sections"][0]["section_id"] == "sec_0"
        assert diagnostics["reranker"]["section"]["enabled"] is True
        assert diagnostics["selector"]["all_rejected"] is True
        assert diagnostics["selector"]["rejection_reason"] == "Aucune section pertinente."
        assert diagnostics["selector_retry"]["triggered"] is True
        assert diagnostics["selector_retry"]["succeeded"] is False
        assert diagnostics["attempts"][1]["search_mode"] == "hybrid"
        assert diagnostics["attempts"][1]["top_k"] == 30

        stage_trace = result.metadata["stage_trace"]
        selector_output = stage_trace["stages"]["context-selector"]["output"]
        assert selector_output["selector_all_rejected"] is True
        assert selector_output["rejection_reason"] == "Aucune section pertinente."
        assert selector_output["selector_decision"] == "all_rejected"
        assert selector_output["selector_retry_triggered"] is True
        assert selector_output["selector_retry_succeeded"] is False
        assert stage_trace["stages"]["retriever"]["output"]["attempts"][1]["name"] == "selector_retry"
        trace_events = result.metadata["rag_trace_events"]
        assert result.metadata["trace_id"]
        assert [event["stage"] for event in trace_events].count("retriever") == 2
        assert any(event["stage"] == "generator" and event["status"] == "skipped_no_context" for event in trace_events)
        # Generator should NOT have been called
        MockGenerator.return_value.generate.assert_not_called()
        assert MockRetriever.return_value.retrieve.call_count == 2
        assert MockAggregator.return_value.aggregate_with_diagnostics.call_count == 2
        assert mock_sel.select.call_count == 2

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_run_stream_selector_all_rejected_sets_last_result(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """Streaming path should also return diagnostics when selector rejects all sections."""
        MockRetriever.return_value.retrieve.return_value = [_make_chunk(0)]
        MockRetriever.return_value.config = MagicMock()
        MockRetriever.return_value.config.tables = ["matte"]

        MockAggregator.return_value.aggregate_with_diagnostics.return_value = _aggregation_result(
            [_make_section(0)],
            before=1,
            after=1,
        )

        mock_sel = MockSelector.return_value
        mock_sel.select.return_value = []
        mock_sel.all_rejected = True
        mock_sel.last_decisions = {}
        mock_sel.last_reasoning = "Aucune section pertinente."
        mock_sel.last_raw_response = "{}"

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        config.retrieval.enable_selector_retry = False
        from assistant_rh_rag_pipeline.pipeline import Pipeline

        pipe = Pipeline(config)
        chunks = list(pipe.run_stream(FAKE_QUERY_RESULT, turn_id="turn123", trace_id="a" * 32))

        assert len(chunks) == 1
        assert "pas trouvé" in chunks[0].lower() or "base de connaissances" in chunks[0].lower()
        assert pipe.last_result is not None
        assert pipe.last_result.context_items == []
        assert pipe.last_result.metadata["selector_all_rejected"] is True
        assert pipe.last_result.metadata["selector_decision"] == "all_rejected"
        assert pipe.last_result.metadata["turn_id"] == "turn123"
        assert pipe.last_result.metadata["trace_id"] == "a" * 32
        assert any(event["stage"] == "retriever" for event in pipe.last_result.metadata["rag_trace_events"])
        assert any(
            event["stage"] == "generator" and event["status"] == "skipped_no_context" for event in pipe.last_result.metadata["rag_trace_events"]
        )
        assert pipe.last_result.metadata["rag_diagnostics"]["selector"]["rejection_reason"] == ("Aucune section pertinente.")
        MockContextBuilder.return_value.build.assert_not_called()
        MockGenerator.return_value.stream.assert_not_called()

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_selector_all_rejected_retries_with_hybrid_search_and_generates_answer(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """When the first selector attempt rejects all sections, a hybrid retry can recover context."""
        MockQueryProcessor.return_value.process.return_value = FAKE_QUERY_RESULT

        initial_chunks = [_make_chunk(0, table="matte")]
        retry_chunks = [_make_chunk(10, table="service_public")]
        mock_retriever = MockRetriever.return_value
        mock_retriever.retrieve.side_effect = [initial_chunks, retry_chunks]
        mock_retriever.config = MagicMock()
        mock_retriever.config.tables = ["matte", "service_public", "dgafp"]
        mock_retriever.config.search_mode = SearchMode.SEMANTIC
        mock_retriever.config.initial_top_k = 15

        initial_sections = [_make_section(0, "MATTE")]
        retry_sections = [_make_section(10, "Service-Public")]
        MockAggregator.return_value.aggregate_with_diagnostics.side_effect = [
            _aggregation_result(initial_sections, before=1, after=1),
            _aggregation_result(retry_sections, before=1, after=1),
        ]

        initial_selector = MagicMock()
        initial_selector.select.return_value = []
        initial_selector.all_rejected = True
        initial_selector.last_decisions = {}
        initial_selector.last_reasoning = "Aucune section pertinente."
        initial_selector.last_raw_response = '{"selected_ids": []}'
        initial_selector.last_prompt_chars = 120

        retry_selector = MagicMock()
        retry_selector.select.return_value = retry_sections
        retry_selector.all_rejected = False
        retry_selector.last_decisions = {"kept": [{"idx": 0, "heading": "Fiche 10"}]}
        retry_selector.last_reasoning = "La seconde recherche trouve une section utile."
        retry_selector.last_raw_response = '{"selected_ids": [0]}'
        retry_selector.last_prompt_chars = 180
        MockSelector.side_effect = [initial_selector, retry_selector]

        context_items = [_make_context_item(10, "Service-Public")]
        MockContextBuilder.return_value.build.return_value = context_items
        MockContextBuilder.return_value.last_full_docs = []
        MockContextBuilder.return_value.last_legal_refs_found = 0
        MockContextBuilder.return_value.last_legal_refs_total = 0
        MockGenerator.return_value.generate.return_value = "Le SFT est versé sous conditions."

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        config.retrieval.selector_retry_top_k = 30
        from assistant_rh_rag_pipeline.pipeline import Pipeline

        pipe = Pipeline(config)
        result = pipe.run_with_trace("Quelles sont les conditions pour recevoir le SFT ?")

        assert result.answer == "Le SFT est versé sous conditions."
        assert result.context_items == context_items
        assert result.metadata["selector_retry_triggered"] is True
        assert result.metadata["selector_retry_succeeded"] is True
        assert result.metadata["selector_all_rejected"] is False
        assert mock_retriever.retrieve.call_count == 2
        initial_call = mock_retriever.retrieve.call_args_list[0]
        assert initial_call.kwargs["tables"] == ["matte", "service_public", "dgafp"]
        assert initial_call.kwargs["force_hybrid_tables"] == {"dgafp"}
        retry_call = mock_retriever.retrieve.call_args_list[1]
        assert retry_call.kwargs["search_mode"] == SearchMode.HYBRID
        assert retry_call.kwargs["top_k"] == 30
        assert retry_call.kwargs["tables"] == ["matte", "service_public", "dgafp"]
        assert retry_call.kwargs["force_hybrid_tables"] == {"dgafp"}
        MockGenerator.return_value.generate.assert_called_once()

        assert result.metadata["tables_searched"] == ["matte", "service_public", "dgafp"]
        attempts = result.metadata["rag_diagnostics"]["attempts"]
        assert [attempt["name"] for attempt in attempts] == ["initial", "selector_retry"]
        assert attempts[0]["tables_searched"] == ["matte", "service_public", "dgafp"]
        assert attempts[0]["selector"]["all_rejected"] is True
        assert attempts[0]["search_mode"] == "semantic"
        assert len(attempts[0]["aggregated_sections"]) == 1
        assert attempts[1]["tables_searched"] == ["matte", "service_public", "dgafp"]
        assert attempts[1]["selector"]["all_rejected"] is False
        assert attempts[1]["search_mode"] == "hybrid"
        assert attempts[1]["top_k"] == 30
        assert len(attempts[1]["aggregated_sections"]) == 1
        assert result.metadata["selector_prompt_chars"] == 300
        assert result.metadata["selector_response_chars"] == len(initial_selector.last_raw_response) + len(retry_selector.last_raw_response)
        assert result.metadata["stage_trace"]["stages"]["context-selector"]["output"]["selector_retry_triggered"] is True

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_selector_retry_preserves_no_answer_when_second_attempt_rejects_all(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """The no-answer behavior is kept when the retry selector also rejects all sections."""
        MockQueryProcessor.return_value.process.return_value = FAKE_QUERY_RESULT

        mock_retriever = MockRetriever.return_value
        mock_retriever.retrieve.side_effect = [[_make_chunk(0)], [_make_chunk(1, table="service_public")]]
        mock_retriever.config = MagicMock()
        mock_retriever.config.tables = ["matte", "service_public"]
        mock_retriever.config.search_mode = SearchMode.SEMANTIC
        mock_retriever.config.initial_top_k = 15

        MockAggregator.return_value.aggregate_with_diagnostics.side_effect = [
            _aggregation_result([_make_section(0)], before=1, after=1),
            _aggregation_result([_make_section(1, "Service-Public")], before=1, after=1),
        ]

        first_selector = MagicMock()
        first_selector.select.return_value = []
        first_selector.all_rejected = True
        first_selector.last_decisions = {}
        first_selector.last_reasoning = "Aucune section pertinente."
        first_selector.last_raw_response = '{"selected_ids": []}'

        retry_selector = MagicMock()
        retry_selector.select.return_value = []
        retry_selector.all_rejected = True
        retry_selector.last_decisions = {}
        retry_selector.last_reasoning = "Toujours aucune section pertinente."
        retry_selector.last_raw_response = '{"selected_ids": []}'
        MockSelector.side_effect = [first_selector, retry_selector]

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        from assistant_rh_rag_pipeline.pipeline import Pipeline

        pipe = Pipeline(config)
        result = pipe.run_with_trace("Question sans contexte utile")

        assert "pas trouvé" in result.answer.lower() or "base de connaissances" in result.answer.lower()
        assert result.context_items == []
        assert result.metadata["selector_retry_triggered"] is True
        assert result.metadata["selector_retry_succeeded"] is False
        assert result.metadata["selector_all_rejected"] is True
        assert result.metadata["selector_rejection_reason"] == "Toujours aucune section pertinente."
        assert mock_retriever.retrieve.call_count == 2
        MockContextBuilder.return_value.build.assert_not_called()
        MockGenerator.return_value.generate.assert_not_called()

        attempts = result.metadata["rag_diagnostics"]["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["selector"]["all_rejected"] is True
        assert attempts[1]["selector"]["all_rejected"] is True

    @patch("assistant_rh_rag_pipeline.pipeline.StreamingGenerator")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextBuilder")
    @patch("assistant_rh_rag_pipeline.pipeline.ContextSelector")
    @patch("assistant_rh_rag_pipeline.pipeline.SectionAggregator")
    @patch("assistant_rh_rag_pipeline.pipeline.Retriever")
    @patch("assistant_rh_rag_pipeline.pipeline.QueryProcessor")
    @patch("assistant_rh_rag_pipeline.pipeline.get_dsn", return_value="postgresql://fake")
    def test_selector_retry_no_answer_when_context_builder_returns_empty(
        self,
        mock_get_dsn,
        MockQueryProcessor,
        MockRetriever,
        MockAggregator,
        MockSelector,
        MockContextBuilder,
        MockGenerator,
    ):
        """No-answer path is taken when retry selector doesn't reject but context builder returns empty."""
        MockQueryProcessor.return_value.process.return_value = FAKE_QUERY_RESULT

        mock_retriever = MockRetriever.return_value
        mock_retriever.retrieve.side_effect = [[_make_chunk(0)], [_make_chunk(1, table="service_public")]]
        mock_retriever.config = MagicMock()
        mock_retriever.config.tables = ["matte", "service_public"]
        mock_retriever.config.search_mode = SearchMode.SEMANTIC
        mock_retriever.config.initial_top_k = 15

        MockAggregator.return_value.aggregate_with_diagnostics.side_effect = [
            _aggregation_result([_make_section(0)], before=1, after=1),
            _aggregation_result([_make_section(1, "Service-Public")], before=1, after=1),
        ]

        first_selector = MagicMock()
        first_selector.select.return_value = []
        first_selector.all_rejected = True
        first_selector.last_decisions = {}
        first_selector.last_reasoning = "Aucune section pertinente."
        first_selector.last_raw_response = '{"selected_ids": []}'

        # Retry selector keeps sections (not all_rejected), but context builder
        # will return empty — this is the edge case from the Codex review.
        retry_selector = MagicMock()
        retry_selector.select.return_value = [_make_section(1, "Service-Public")]
        retry_selector.all_rejected = False
        retry_selector.last_decisions = {"kept": [{"idx": 0}]}
        retry_selector.last_reasoning = "Une section est pertinente."
        retry_selector.last_raw_response = '{"selected_ids": [0]}'
        MockSelector.side_effect = [first_selector, retry_selector]

        # Context builder returns empty despite selector keeping sections
        MockContextBuilder.return_value.build.return_value = []
        MockContextBuilder.return_value.last_full_docs = []
        MockContextBuilder.return_value.last_legal_refs_found = 0
        MockContextBuilder.return_value.last_legal_refs_total = 0
        MockGenerator.return_value.generate.return_value = "Réponse."

        config = RAGConfig()
        config.selector = SelectorConfig(enabled=True)
        from assistant_rh_rag_pipeline.pipeline import Pipeline

        pipe = Pipeline(config)
        result = pipe.run_with_trace("Question où le contexte est vide après retry")

        # Even though the retry selector didn't explicitly reject, the no-answer
        # path must be taken because the retry produced no usable context.
        assert "pas trouvé" in result.answer.lower() or "base de connaissances" in result.answer.lower()
        assert result.context_items == []
        assert result.metadata["selector_retry_triggered"] is True
        assert result.metadata["selector_retry_succeeded"] is False
        assert result.metadata["selector_all_rejected"] is True
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

        MockAggregator.return_value.aggregate_with_diagnostics.return_value = _aggregation_result(
            [_make_section(0)],
            before=1,
            after=1,
        )

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
        result = pipe.run_with_trace("Qu'est-ce que le congé de mobilité ?", turn_id="turn123", trace_id="a" * 32)

        stage_trace = result.metadata.get("stage_trace")
        assert isinstance(stage_trace, dict)
        assert stage_trace.get("schema_version") == "2026-05-05"
        assert result.metadata["turn_id"] == "turn123"
        assert result.metadata["trace_id"] == "a" * 32
        assert [event["stage"] for event in result.metadata["rag_trace_events"]] == [
            "query-processor",
            "retriever",
            "section-aggregator",
            "context-selector",
            "context-builder",
            "generator",
        ]

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
