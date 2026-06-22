#!/usr/bin/env python3
"""
Test that chat_runs INSERT works with realistic V3 pipeline data.

Tests the ``chat_logger`` module directly (build_log_row, build_non_rag_row,
_build_upsert_sql, SafeEncoder, log_run).

Usage:
    pytest tests/test_chat_logging.py -v
    python tests/test_chat_logging.py          # standalone
    python tests/test_chat_logging.py --with-db # includes live DB round-trip
"""

import datetime as dt
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

try:
    import pytest
except ImportError:
    pytest = None

sys.path.insert(0, str(Path(__file__).parent.parent))

from assistant_rh_rag_pipeline.chat_logger import (
    SafeEncoder,
    _build_upsert_sql,
    _jdumps,
    _prepare_data,
    build_log_row,
    build_non_rag_row,
    log_run,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MOCK PIPELINE OBJECTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_uuid():
    return uuid.uuid4()


class _Intent:
    def __init__(self, value: str):
        self.value = value


@dataclass
class _MockContextItem:
    publisher: str = "MATTE"
    token_estimate: int = 400
    references_juridiques: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class _MockResult:
    metadata: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)
    context_items: List[Any] = field(default_factory=list)


@dataclass
class _MockQR:
    intent: Any = None
    intent_confidence: float = 0.95
    intent_reason: str = "rag_query"
    intent_raw_response: str = '{"intent":"rag_query"}'
    should_proceed: bool = True
    needs_legal_search: bool = False
    needs_legal_search_llm: Optional[bool] = None
    query_for_retrieval: str = "droits RTT"
    expanded_acronyms: List[str] = field(default_factory=list)
    detected_acronyms: Dict[str, str] = field(default_factory=dict)
    was_enriched: bool = False
    enriched_query: str = ""
    theme: Optional[str] = None
    processed_query: str = "droits RTT"


@dataclass
class _MockSelectorConfig:
    model: str = "openweight-large"
    prompt_name: str = "selector_v3"
    enabled: bool = True


@dataclass
class _MockGenerationConfig:
    system_prompt_name: str = "system_prompt_V6"
    model: str = "openweight-large"
    temperature: float = 0.15


@dataclass
class _MockQPConfig:
    enable_intent_gating: bool = True
    enable_acronym_expansion: bool = True


@dataclass
class _MockAggConfig:
    enable_section_reranker: bool = True


@dataclass
class _MockConfig:
    selector: _MockSelectorConfig = field(default_factory=_MockSelectorConfig)
    generation: _MockGenerationConfig = field(default_factory=_MockGenerationConfig)
    query_processor: _MockQPConfig = field(default_factory=_MockQPConfig)
    aggregation: _MockAggConfig = field(default_factory=_MockAggConfig)


class _MockContextBuilder:
    last_resolved_refs = {"L332-2": {"cid": "LEGIARTI000044426716", "title": "CGFP"}}


class _MockPipeline:
    last_result: _MockResult
    last_full_prompt: str = "You are an HR assistant"
    last_system_prompt: str = "System prompt"
    _context_builder = _MockContextBuilder()

    def __init__(self, result: _MockResult):
        self.last_result = result
        self._timing = result.timing


class _MockRuntimeConfig:
    llm_provider = "albert"
    llm_model = "openweight-large"
    temperature = 0.15
    embedding_model = "openweight-embeddings"
    reranker_name = "openweight-rerank"
    v3_context_mode = "standard"
    v3_enable_selector = True
    v3_enable_reranker = True
    v3_search_mode = "semantic"
    v3_initial_top_k = 10
    v3_rerank_top_k = 5
    v3_enable_escalation = True


def _build_mock_objects():
    """Build a complete set of mock pipeline objects."""
    items = [
        _MockContextItem(publisher="MATTE", token_estimate=800, metadata={"is_doc_entire": True}),
        _MockContextItem(publisher="Service-Public", token_estimate=400, metadata={}),
    ]
    result = _MockResult(
        metadata={
            "aggregated_sections": [
                {"publisher": "MATTE", "heading": "Congés", "idx": 0},
                {"publisher": "Service-Public", "heading": "CDD", "idx": 1},
            ],
            "selector_decisions": {
                "kept": [{"idx": 0, "publisher": "MATTE", "heading": "Congés"}],
                "removed": [{"idx": 1, "publisher": "Service-Public", "heading": "CDD"}],
            },
            "selector_items_before": 2,
            "selector_items_after": 1,
            "selector_reasoning": "Section 0 is relevant",
            "retrieved_chunks": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
            "context_items_ref": [{"section_id": "s1", "heading": "Congés"}],
            "sections_before_rerank": 5,
            "sections_after_rerank": 3,
            "reranker_status": {
                "chunk": {"enabled": False, "status": "disabled", "top_k": 0},
                "section": {
                    "enabled": True,
                    "status": "completed",
                    "top_k": 5,
                    "items_before": 5,
                    "items_after": 3,
                    "error": "",
                },
            },
        },
        timing={
            "query_processing_ms": 150.5,
            "retrieval_ms": 320.2,
            "aggregation_ms": 45.1,
            "context_build_ms": 12.3,
            "selector_ms": 280.0,
            "generation_ms": 1500.7,
            "ttft_ms": 200,
            "chars_per_second": 45.2,
            "response_length_tokens": 350,
        },
        context_items=items,
    )
    pipeline = _MockPipeline(result)
    qr = _MockQR(
        intent=_Intent("rag_query"),
        expanded_acronyms=["RTT"],
        detected_acronyms={"RTT": "Réduction du Temps de Travail"},
        was_enriched=True,
        enriched_query="droits RTT agent contractuel",
        theme="conges",
    )
    config = _MockConfig()
    runtime = _MockRuntimeConfig()
    v1_chunks = [MagicMock(id="chunk1", metadata={"source_name": "MATTE"})]
    return pipeline, qr, config, runtime, items, v1_chunks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – SafeEncoder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSafeEncoder:
    def test_uuid_serialized(self):
        data = {"id": _make_uuid(), "nested": [{"uid": _make_uuid()}]}
        result = json.dumps(data, cls=SafeEncoder)
        parsed = json.loads(result)
        assert isinstance(parsed["id"], str)
        assert len(parsed["id"]) == 36

    def test_raw_uuid_fails_without_encoder(self):
        with (pytest.raises(TypeError) if pytest else None) or _raises(TypeError):
            json.dumps({"id": _make_uuid()})

    def test_jdumps_helper(self):
        uid = _make_uuid()
        result = _jdumps({"key": uid})
        parsed = json.loads(result)
        assert parsed["key"] == str(uid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – Dynamic SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDynamicSQL:
    def test_basic_upsert(self):
        sql = _build_upsert_sql({"turn_id": "abc", "question": "hello", "filters": "{}"})
        assert "INSERT INTO chat_runs" in sql
        assert "ON CONFLICT (turn_id)" in sql
        assert "CAST(:filters AS jsonb)" in sql
        assert "turn_id = EXCLUDED.turn_id" not in sql  # turn_id excluded from UPDATE

    def test_table_column_quoted(self):
        sql = _build_upsert_sql({"turn_id": "x", "table": "t"})
        assert '"table"' in sql

    def test_jsonb_cast_applied(self):
        for col in ("v3_timing_breakdown", "v3_context_items_summary", "v3_selector_decisions"):
            sql = _build_upsert_sql({"turn_id": "x", col: "[]"})
            assert f"CAST(:{col} AS jsonb)" in sql

    def test_non_jsonb_no_cast(self):
        sql = _build_upsert_sql({"turn_id": "x", "question": "q"})
        assert "CAST(:question" not in sql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – build_log_row
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildLogRow:
    REQUIRED_COLUMNS = [
        "ts",
        "turn_id",
        "question",
        "answer",
        "backend",
        "filters",
        "top_k",
        "use_reranker",
        "session_id",
        "conversation_id",
        "rag_version",
        "user_group",
        "v3_context_mode",
        "v3_sections_count",
        "v3_context_items_count",
        "v3_chunks_retrieved_count",
        "v3_intent",
        "v3_source_distribution",
        "v3_timing_breakdown",
        "v3_query_processing_ms",
        "v3_retrieval_ms",
        "v3_generation_ms",
        "v3_reranker_status",
        "v3_needs_legal_llm",
        "v3_needs_legal_final",
    ]

    def _build_row(self, metadata_overrides: Optional[dict] = None):
        pipeline, qr, config, runtime, items, v1_chunks = _build_mock_objects()
        if metadata_overrides:
            pipeline.last_result.metadata.update(metadata_overrides)
        return build_log_row(
            turn_id="abc12345",
            query="Quels sont mes droits RTT ?",
            response="En tant que contractuel, vous avez droit à...",
            pipeline=pipeline,
            qr=qr,
            config=config,
            runtime_config=runtime,
            session_state={"session_id": "s1", "conversation_id": "c1", "turns": []},
            total_time_ms=2500.0,
            context_items=items,
            v1_chunks_for_display=v1_chunks,
            legal_refs_v3=[],
        )

    def test_all_required_columns_present(self):
        row = self._build_row()
        missing = [c for c in self.REQUIRED_COLUMNS if c not in row]
        assert not missing, f"Missing columns: {missing}"

    def test_json_columns_valid(self):
        row = self._build_row()
        for key, val in row.items():
            if isinstance(val, str) and val.startswith(("{", "[")):
                try:
                    json.loads(val)
                except json.JSONDecodeError:
                    raise AssertionError(f"Column '{key}' has invalid JSON: {val[:100]}")

    def test_doc_entire_count(self):
        row = self._build_row()
        assert row["v3_doc_entire_count"] == 1

    def test_legal_refs_from_resolved(self):
        row = self._build_row()
        assert row["v3_legal_refs_from_dgafp"] == 1
        details = json.loads(row["v3_legal_refs_details"])
        assert len(details) == 1
        assert details[0]["number"] == "L332-2"

    def test_selector_confidence_ratio(self):
        row = self._build_row()
        assert row["v3_selector_confidence"] == 0.5  # 1/2

    def test_timing_keys(self):
        row = self._build_row()
        assert row["v3_query_processing_ms"] == 150
        assert row["v3_retrieval_ms"] == 320
        assert row["v3_context_building_ms"] == 12
        assert row["v3_selector_ms"] == 280
        assert row["v3_generation_ms"] == 1500

    def test_acronyms_formatted(self):
        row = self._build_row()
        assert "RTT" in row["v3_acronyms_expanded"]
        assert "detected:" in row["v3_acronyms_expanded"]

    def test_reranker_status_completed(self):
        row = self._build_row()
        assert row["v3_reranker_status"] == "completed"
        assert row["v3_reranker_error"] == ""

    def test_reranker_failure_visible(self):
        row = self._build_row(
            metadata_overrides={
                "reranker_status": {
                    "section": {
                        "enabled": True,
                        "status": "failed",
                        "error": "422 Client Error: Unprocessable Entity",
                    },
                },
            }
        )
        assert row["v3_reranker_status"] == "failed"
        assert "422" in row["v3_reranker_error"]

    def test_reranker_status_missing_metadata(self):
        row = self._build_row(metadata_overrides={"reranker_status": {}})
        assert row["v3_reranker_status"] == ""
        assert row["v3_reranker_error"] == ""

    def test_reranker_status_section_none(self):
        row = self._build_row(metadata_overrides={"reranker_status": {"section": None}})
        assert row["v3_reranker_status"] == ""
        assert row["v3_reranker_error"] == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – build_non_rag_row
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildNonRagRow:
    def _build_row(self):
        pipeline = _MockPipeline(_MockResult())
        pipeline._timing = {"query_processing_ms": 120}
        qr = _MockQR(
            intent=_Intent("chit_chat"),
            intent_confidence=0.98,
            should_proceed=False,
            theme="",
            expanded_acronyms=[],
        )
        return build_non_rag_row(
            turn_id="xyz789",
            query="Bonjour !",
            response="Bonjour, comment puis-je vous aider ?",
            qr=qr,
            pipeline=pipeline,
            session_state={"session_id": "s1", "conversation_id": "c1", "turns": []},
        )

    def test_backend_is_intent_gating(self):
        row = self._build_row()
        assert row["backend"] == "intent_gating"

    def test_should_proceed_false(self):
        row = self._build_row()
        assert row["v3_should_proceed"] is False

    def test_intent_value(self):
        row = self._build_row()
        assert row["v3_intent"] == "chit_chat"

    def test_required_fields_present(self):
        row = self._build_row()
        required = ["ts", "turn_id", "question", "answer", "session_id", "conversation_id", "turn_index", "rag_version", "v3_intent"]
        missing = [c for c in required if c not in row]
        assert not missing, f"Missing: {missing}"

    def test_serializable(self):
        row = self._build_row()
        for key, val in row.items():
            if isinstance(val, str) and val.startswith(("{", "[")):
                json.loads(val)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – _prepare_data
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPrepareData:
    def test_dict_filters_serialized(self):
        out = _prepare_data({"filters": {"a": 1}})
        assert isinstance(out["filters"], str)
        assert json.loads(out["filters"]) == {"a": 1}

    def test_none_jsonb_becomes_empty(self):
        out = _prepare_data({"v3_timing_breakdown": None})
        assert out["v3_timing_breakdown"] == "[]"

    def test_missing_defaults_added(self):
        out = _prepare_data({})
        assert out.get("rag_version") is None
        assert out.get("user_group") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – log_run (mocked DB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLogRun:
    def test_calls_db_when_engine_provided(self):
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda _: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        row = {"turn_id": "abc", "ts": "2026-01-01", "question": "hello"}
        log_run(row, engine=mock_engine)

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_csv_fallback_on_no_engine(self, tmp_path):
        csv_path = tmp_path / "test.csv"
        row = {"turn_id": "abc", "question": "hello"}
        log_run(row, engine=None, csv_path=csv_path, csv_fields=["turn_id", "question"])
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "abc" in content

    def test_csv_fallback_on_db_error(self, tmp_path):
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("connection refused")

        csv_path = tmp_path / "test.csv"
        row = {"turn_id": "abc", "question": "hello"}
        log_run(row, engine=mock_engine, csv_path=csv_path, csv_fields=["turn_id", "question"])
        assert csv_path.exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – Acronym Detection (unchanged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAcronymDetection:
    def _make_processor(self, acronyms: dict):
        from assistant_rh_rag_pipeline.config import QueryProcessorConfig
        from assistant_rh_rag_pipeline.query_processor import QueryProcessor

        config = QueryProcessorConfig(enable_acronym_expansion=False, enable_intent_gating=False)
        proc = QueryProcessor(config, verbose=False)
        proc._acronyms = acronyms
        return proc

    def test_detect_single_acronym(self):
        proc = self._make_processor({"RTT": "Réduction du Temps de Travail", "ARE": "Aide au Retour à l'Emploi"})
        detected = proc._detect_acronyms("Est-ce qu'il peut toucher l'ARE ?")
        assert "ARE" in detected
        assert detected["ARE"] == "Aide au Retour à l'Emploi"

    def test_detect_multiple_acronyms(self):
        proc = self._make_processor({"RTT": "Réduction du Temps de Travail", "CDD": "Contrat à Durée Déterminée"})
        detected = proc._detect_acronyms("Un CDD peut-il avoir des RTT ?")
        assert "CDD" in detected and "RTT" in detected

    def test_no_false_positive_lowercase(self):
        proc = self._make_processor({"ARE": "Aide au Retour à l'Emploi"})
        detected = proc._detect_acronyms("les aires de repos sont-elles accessibles ?")
        assert "ARE" not in detected

    def test_no_detection_when_absent(self):
        proc = self._make_processor({"RTT": "Réduction du Temps de Travail"})
        detected = proc._detect_acronyms("Quel est mon droit aux congés ?")
        assert len(detected) == 0

    def test_process_without_intent_gating_expands_inline(self):
        proc = self._make_processor({"ARE": "Aide au Retour à l'Emploi"})
        result = proc.process("Peut-on toucher l'ARE ?")
        assert result.detected_acronyms.get("ARE")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – Feedback logging (log_feedback_row)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_feedback_row(**overrides) -> dict:
    """Build a realistic feedback row dict."""
    base = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "turn_id": str(uuid.uuid4())[:8],
        "turn_idx": 0,
        "helpful": True,
        "reasons": "Clair; Utile",
        "reasons_positive": "Clair; Utile",
        "reasons_negative": "",
        "comment": "Bonne réponse",
        "stars": 4,
        "session_id": "test-session",
        "question": "Qu'est-ce que le RIFSEEP ?",
        "answer": "Le RIFSEEP est le régime indemnitaire...",
    }
    base.update(overrides)
    return base


class TestFeedbackLogging:
    """Test log_feedback_row from src.ui.chatbot_logging."""

    def test_feedback_row_has_required_fields(self):
        """All required DB columns are present in a feedback row."""
        row = _make_feedback_row()
        required = {
            "ts",
            "turn_id",
            "turn_idx",
            "helpful",
            "reasons",
            "comment",
            "stars",
            "reasons_positive",
            "reasons_negative",
            "session_id",
            "question",
            "answer",
        }
        assert required.issubset(set(row.keys()))

    def test_feedback_row_stars_in_range(self):
        """Stars value must be 0-4 (st.feedback native range)."""
        for stars in range(5):
            row = _make_feedback_row(stars=stars)
            assert 0 <= row["stars"] <= 4

    def test_feedback_calls_db_insert(self):
        """log_feedback_row should execute INSERT on the engine."""
        from src.ui.chatbot_logging import log_feedback_row

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda _: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        row = _make_feedback_row()

        with patch("src.ui.chatbot_logging.get_engine", return_value=mock_engine):
            with patch("src.ui.chatbot_logging._auto_enrich_goldset"):
                log_feedback_row(row)

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_feedback_csv_fallback_on_no_engine(self, tmp_path):
        """Without DB, feedback should be written to CSV."""
        from src.ui.chatbot_logging import log_feedback_row

        csv_path = tmp_path / "feedbacks.csv"
        row = _make_feedback_row()

        with patch("src.ui.chatbot_logging.get_engine", return_value=None), patch("src.ui.chatbot_logging.FEEDS_CSV", csv_path):
            log_feedback_row(row)

        assert csv_path.exists()
        content = csv_path.read_text()
        assert row["turn_id"] in content

    def test_feedback_negative_has_reasons(self):
        """A negative feedback (1 star) should have negative reasons."""
        row = _make_feedback_row(
            stars=0,
            helpful=False,
            reasons="Confus; Incomplet",
            reasons_positive="",
            reasons_negative="Confus; Incomplet",
        )
        assert row["helpful"] is False
        assert "Confus" in row["reasons_negative"]
        assert row["reasons_positive"] == ""

    def test_feedback_positive_has_no_negative_reasons(self):
        """A 5-star feedback should only have positive reasons."""
        row = _make_feedback_row(
            stars=4,
            helpful=True,
            reasons="Clair; Précis",
            reasons_positive="Clair; Précis",
            reasons_negative="",
        )
        assert row["helpful"] is True
        assert row["reasons_negative"] == ""
        assert "Clair" in row["reasons_positive"]

    def test_goldset_enrichment_called(self):
        """Goldset auto-enrichment should be triggered after successful DB insert."""
        from src.ui.chatbot_logging import log_feedback_row

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda _: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        row = _make_feedback_row()

        with (
            patch("src.ui.chatbot_logging.get_engine", return_value=mock_engine),
            patch("src.ui.chatbot_logging._auto_enrich_goldset") as mock_enrich,
        ):
            log_feedback_row(row)

        mock_enrich.assert_called_once_with(mock_engine, row)

    def test_goldset_enrichment_failure_does_not_break_feedback(self):
        """If goldset enrichment fails, feedback should still be logged."""
        from src.ui.chatbot_logging import log_feedback_row

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda _: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        row = _make_feedback_row()

        with (
            patch("src.ui.chatbot_logging.get_engine", return_value=mock_engine),
            patch("src.ui.chatbot_logging._auto_enrich_goldset", side_effect=RuntimeError("boom")),
        ):
            log_feedback_row(row)

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TESTS – DB integration (requires active tunnel)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _db_available() -> bool:
    """Check if the configured DB is reachable (auto-detect, no CLI flag needed)."""
    from dotenv import load_dotenv

    load_dotenv()
    url = os.getenv("SCW_POSTGRES_DSN") or os.getenv("APP_POSTGRES_DSN") or os.getenv("STREAMLIT_POSTGRES_DSN")
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if "://" in url and "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="No reachable DB tunnel")
class TestDBRoundTrip:
    def test_column_existence(self):
        from dotenv import load_dotenv

        load_dotenv()

        url = os.getenv("SCW_POSTGRES_DSN") or os.getenv("APP_POSTGRES_DSN") or os.getenv("STREAMLIT_POSTGRES_DSN")
        if not url:
            raise RuntimeError("No DB URL configured")

        from sqlalchemy import create_engine, text

        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        if "sslmode=" not in url:
            url += "&sslmode=require" if "?" in url else "?sslmode=require"

        engine = create_engine(url, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            cols = conn.execute(
                text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'chat_runs' ORDER BY ordinal_position
            """)
            )
            db_columns = {r[0] for r in cols}

        pipeline, qr, config, runtime, items, v1_chunks = _build_mock_objects()
        row = build_log_row(
            turn_id="test_col_check",
            query="test",
            response="test",
            pipeline=pipeline,
            qr=qr,
            config=config,
            runtime_config=runtime,
            session_state={"session_id": "s", "conversation_id": "c", "turns": []},
            total_time_ms=0,
            context_items=items,
            v1_chunks_for_display=v1_chunks,
            legal_refs_v3=[],
        )

        missing = [c for c in row if c.startswith("v3_") and c not in db_columns]
        if missing:
            raise AssertionError(f"Columns in code but missing in DB: {missing}")
        print(f"  ✅ All {len([c for c in row if c.startswith('v3_')])} V3 columns validated")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STANDALONE RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _raises:
    """Minimal pytest.raises replacement for standalone mode."""

    def __init__(self, exc_type):
        self.exc_type = exc_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"Expected {self.exc_type.__name__}")
        if not issubclass(exc_type, self.exc_type):
            return False
        return True


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Chat Logging Tests (chat_logger module)")
    print("=" * 60)

    passed = 0
    failed = 0

    tests = [
        ("SafeEncoder: UUID serialized", TestSafeEncoder().test_uuid_serialized),
        ("SafeEncoder: jdumps helper", TestSafeEncoder().test_jdumps_helper),
        ("SQL: basic upsert", TestDynamicSQL().test_basic_upsert),
        ("SQL: table quoted", TestDynamicSQL().test_table_column_quoted),
        ("SQL: JSONB cast", TestDynamicSQL().test_jsonb_cast_applied),
        ("SQL: non-JSONB no cast", TestDynamicSQL().test_non_jsonb_no_cast),
        ("build_log_row: required columns", TestBuildLogRow().test_all_required_columns_present),
        ("build_log_row: JSON valid", TestBuildLogRow().test_json_columns_valid),
        ("build_log_row: doc_entire count", TestBuildLogRow().test_doc_entire_count),
        ("build_log_row: legal refs", TestBuildLogRow().test_legal_refs_from_resolved),
        ("build_log_row: selector confidence", TestBuildLogRow().test_selector_confidence_ratio),
        ("build_log_row: timing keys", TestBuildLogRow().test_timing_keys),
        ("build_log_row: acronyms", TestBuildLogRow().test_acronyms_formatted),
        ("build_log_row: reranker status", TestBuildLogRow().test_reranker_status_completed),
        ("build_log_row: reranker failure visible", TestBuildLogRow().test_reranker_failure_visible),
        ("build_non_rag_row: backend", TestBuildNonRagRow().test_backend_is_intent_gating),
        ("build_non_rag_row: should_proceed", TestBuildNonRagRow().test_should_proceed_false),
        ("build_non_rag_row: intent", TestBuildNonRagRow().test_intent_value),
        ("build_non_rag_row: required fields", TestBuildNonRagRow().test_required_fields_present),
        ("build_non_rag_row: serializable", TestBuildNonRagRow().test_serializable),
        ("prepare_data: dict filters", TestPrepareData().test_dict_filters_serialized),
        ("prepare_data: none jsonb", TestPrepareData().test_none_jsonb_becomes_empty),
        ("Acronyms: single detection", TestAcronymDetection().test_detect_single_acronym),
        ("Acronyms: multiple detection", TestAcronymDetection().test_detect_multiple_acronyms),
        ("Acronyms: no false positive", TestAcronymDetection().test_no_false_positive_lowercase),
        ("Acronyms: no detection when absent", TestAcronymDetection().test_no_detection_when_absent),
        ("Acronyms: process expands inline", TestAcronymDetection().test_process_without_intent_gating_expands_inline),
    ]

    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1

    if "--with-db" in sys.argv:
        print("\n🔌 Running DB column validation...")
        try:
            TestDBRoundTrip().test_column_existence()
            passed += 1
        except Exception as e:
            print(f"  ❌ DB column check: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"{'✅' if failed == 0 else '❌'} Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed > 0 else 0)
