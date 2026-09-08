from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest
from assistant_rh_rag_pipeline.models import AggregatedSection, ContextItem, RetrievedChunk
from assistant_rh_rag_pipeline.tracing import (
    _build_otlp_payload,
    _evidence_attributes,
    _resolve_otlp_traces_endpoint,
    _send_otlp_payload,
    bounded_preview,
    chunk_ref,
    context_item_ref,
    export_events_to_otel,
    make_trace_event,
    normalize_trace_id,
    section_ref,
)


@pytest.mark.parametrize("metadata_key", ["doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid"])
def test_document_identity_reaches_otel_evidence_at_every_stage(metadata_key: str) -> None:
    metadata = {metadata_key: "DOC-123"}
    chunk = RetrievedChunk("c1", "Extrait", 0.8, "rag_chunks_dgafp", metadata=metadata)
    section = AggregatedSection(None, "Titre", "Section", [chunk], 0.9, metadata=metadata)
    context = ContextItem(None, "Titre", "Contexte", 0.9, metadata=metadata)
    refs = [chunk_ref(chunk), section_ref(section), context_item_ref(context)]
    assert all(ref["document_id"] == "DOC-123" for ref in refs)
    events = [
        make_trace_event(stage="retriever", output_ref={"retrieved_chunks": [refs[0]]}),
        make_trace_event(stage="section-aggregator", output_ref={"aggregated_sections": [refs[1]]}),
        make_trace_event(stage="context-selector", output_ref={"candidate_sections": [dict(refs[1], selection_state="kept")]}),
        make_trace_event(stage="context-builder", output_ref={"context_items": [refs[2]]}),
        make_trace_event(stage="generator", input_ref={"context_items": [refs[2]]}),
    ]
    payload = _build_otlp_payload(turn_id="turn", trace_id="a" * 32, events=events, env_label="staging")
    for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1:]:
        attrs = {attr["key"]: next(iter(attr["value"].values())) for attr in span["attributes"]}
        assert "Document : DOC-123" in attrs["rag.evidence"], span["name"]


@pytest.mark.parametrize("metadata_key", ["cid", "short_id"])
def test_standalone_section_keeps_chunk_document_identity_without_exporting_chunks(metadata_key: str) -> None:
    chunk = RetrievedChunk("c1", "Extrait", 0.8, "rag_chunks_dgafp", metadata={metadata_key: "LEGI-123"})
    section = AggregatedSection(None, "Titre", "Section", [chunk], 0.9)
    ref = section_ref(section, include_chunks=False)
    assert ref["document_id"] == "LEGI-123"
    assert "chunks" not in ref
    evidence = _evidence_attributes(make_trace_event(stage="context-selector", output_ref={"selected_sections": [ref]}))
    assert "Document : LEGI-123" in evidence["rag.evidence"]


def test_trace_document_identity_uses_canonical_precedence_and_allows_missing_ids() -> None:
    metadata = {"doc_id": "canonical", "source_document_id": "alternate"}
    chunk = RetrievedChunk("c1", "Extrait", 0.8, "rag_chunks_dgafp", metadata=metadata)
    context = ContextItem(None, "Titre", "Contexte", 0.9, metadata=metadata)
    section = AggregatedSection(None, "Titre", "Section", [chunk], 0.9, metadata=metadata)
    assert chunk_ref(chunk)["document_id"] == "canonical"
    assert context_item_ref(context)["document_id"] == "canonical"
    assert section_ref(section)["document_id"] == "canonical"
    section.document_id = "section-document"
    assert section_ref(section)["document_id"] == "section-document"
    chunk.metadata = {}
    context.metadata = {}
    section.metadata = {}
    section.document_id = None
    assert chunk_ref(chunk)["document_id"] == ""
    assert context_item_ref(context)["document_id"] == ""
    assert section_ref(section)["document_id"] == ""


def test_normalize_trace_id_accepts_valid_hex() -> None:
    trace_id = "a" * 32
    assert normalize_trace_id(trace_id) == trace_id


def test_normalize_trace_id_hashes_arbitrary_values() -> None:
    trace_id = normalize_trace_id("turn-123")
    assert len(trace_id) == 32
    assert trace_id != "turn-123"


def test_bounded_preview_collapses_whitespace_and_truncates() -> None:
    assert bounded_preview("a\n\nb\tc", 10) == "a b c"
    assert bounded_preview("x" * 20, 8) == "xxxxx..."


def test_make_trace_event_bounds_error_message() -> None:
    event = make_trace_event(stage="retriever", error_message="x" * 3000)
    assert event["stage"] == "retriever"
    assert len(event["error_message"]) <= 2000


def test_otlp_payload_uses_openinference_kinds_and_compacts_previews() -> None:
    event = make_trace_event(
        stage="retriever",
        duration_ms=15,
        output_ref={
            "retrieved_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "table": "rag_chunks_matte",
                    "score": 0.92,
                    "preview": "full text preview should stay out of OTEL output.value",
                }
            ]
        },
    )

    payload = _build_otlp_payload(turn_id="turn1", trace_id="b" * 32, events=[event], env_label="staging")
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    retriever_span = next(span for span in spans if span["name"] == "rag.retriever")
    attrs = {attr["key"]: next(iter(attr["value"].values())) for attr in retriever_span["attributes"]}

    assert attrs["openinference.span.kind"] == "RETRIEVER"
    assert attrs["rag.turn_id"] == "turn1"
    assert attrs["rag.chunk_ids"] == "chunk-1"
    assert "preview" not in attrs["output.value"]
    assert "full text preview should stay out of OTEL output.value" in attrs["rag.evidence"]
    assert attrs["rag.evidence.total"] == "1"


def test_evidence_preserves_chunk_and_section_scores_and_actual_selection() -> None:
    candidates = [
        {
            "section_id": "s1",
            "document_id": "d1",
            "heading": "Congés",
            "score": 0.8,
            "selection_state": "kept",
            "chunks": [
                {"chunk_id": "c1", "score": 0.32, "table": "rag_chunks_mso", "preview": "Texte du chunk 1"},
                {"chunk_id": "c2", "score": 0.21, "preview": "Texte du chunk 2"},
            ],
        },
        {"section_id": "s2", "selection_state": "removed", "preview": "Autre section"},
    ]
    event = make_trace_event(stage="context-selector", output_ref={"candidate_sections": candidates, "reason": "Motif global"})
    original = copy.deepcopy(event)
    attrs = _evidence_attributes(event)
    assert attrs["rag.evidence.total"] == 3
    assert attrs["rag.evidence.shown"] == 3
    assert attrs["rag.evidence.truncated"] is False
    assert "Score chunk : 0.32" in attrs["rag.evidence"]
    assert "Score section : 0.8" in attrs["rag.evidence"]
    assert "Texte du chunk 2" in attrs["rag.evidence"]
    assert "Sélection : Retenu" in attrs["rag.evidence"]
    assert "Sélection : Écarté" in attrs["rag.evidence"]
    assert attrs["rag.selection.reason"] == "Motif global"
    assert event == original


def test_evidence_bounds_multibyte_text_and_reports_omissions() -> None:
    chunks = [{"chunk_id": str(i), "preview": "é" * 2_000} for i in range(100)]
    attrs = _evidence_attributes(make_trace_event(stage="retriever", output_ref={"retrieved_chunks": chunks}))
    assert len(attrs["rag.evidence"].encode("utf-8")) <= 16_000
    assert "é" * 500 not in attrs["rag.evidence"]
    assert attrs["rag.evidence.total"] == 100
    assert 0 < attrs["rag.evidence.shown"] < 100
    assert attrs["rag.evidence.truncated"] is True
    assert "Affichage limité" in attrs["rag.evidence"]


def test_generator_evidence_uses_its_actual_input_and_keeps_diagnostic_json_compact() -> None:
    event = make_trace_event(
        stage="generator",
        input_ref={"context_items": [{"section_id": "s-final", "preview": "Contexte réellement fourni", "is_doc_entire": True}]},
        output_ref={"answer_preview": "Réponse"},
    )
    payload = _build_otlp_payload(turn_id="turn", trace_id="e" * 32, events=[event], env_label="prod")
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    attrs = {attr["key"]: next(iter(attr["value"].values())) for attr in span["attributes"]}
    assert "Contexte réellement fourni" in attrs["rag.evidence"]
    assert "document entier" in attrs["rag.evidence"]
    assert "context_items" not in json.loads(attrs["input.value"])


def test_selector_candidates_do_not_displace_existing_otel_diagnostics() -> None:
    event = make_trace_event(
        stage="context-selector",
        output_ref={
            "candidate_sections": [
                {"heading": "title" * 15, "chunks": [{"chunk_id": f"{i}-{j}", "preview": "é" * 500, "heading": "title" * 15} for j in range(2)]}
                for i in range(20)
            ],
            "reason": "Motif préservé",
            "selector_decisions": {"kept": ["s1"]},
            "selected_sections": [{"section_id": "s1", "chunks": [{"chunk_id": "c1", "preview": "extrait"}]}],
        },
    )
    payload = _build_otlp_payload(turn_id="turn", trace_id="e" * 32, events=[event], env_label="prod")
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    attrs = {attr["key"]: next(iter(attr["value"].values())) for attr in span["attributes"]}
    diagnostic = json.loads(attrs["output.value"])
    assert diagnostic == {"reason": "Motif préservé", "selector_decisions": {"kept": ["s1"]}, "selected_sections": [{"section_id": "s1"}]}
    assert attrs["rag.evidence.total"] == "40"


def test_missing_evidence_is_distinct_from_empty_context_and_attempts_stay_separate() -> None:
    assert _evidence_attributes(make_trace_event(stage="query-processor")) == {}
    assert _evidence_attributes(make_trace_event(stage="generator", input_ref={"context_item_count": 2})) == {}
    empty = _evidence_attributes(make_trace_event(stage="context-builder", output_ref={"context_items": []}))
    assert empty["rag.evidence.total"] == 0
    events = [
        make_trace_event(stage="retriever", attempt_name=attempt, output_ref={"retrieved_chunks": [{"chunk_id": attempt, "preview": attempt}]})
        for attempt in ("initial", "selector_retry")
    ]
    payload = _build_otlp_payload(turn_id="turn", trace_id="f" * 32, events=events, env_label="prod")
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1:]
    for span, attempt in zip(spans, ("initial", "selector_retry"), strict=True):
        attrs = {attr["key"]: next(iter(attr["value"].values())) for attr in span["attributes"]}
        assert attrs["rag.attempt_name"] == attempt
        assert f"Chunk : {attempt}" in attrs["rag.evidence"]


def test_export_events_to_otel_noops_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("RAG_TRACING_ENABLED", raising=False)
    with patch("assistant_rh_rag_pipeline.tracing.requests.post") as mock_post:
        export_events_to_otel(turn_id="turn1", trace_id="c" * 32, events=[], env_label="staging")

    mock_post.assert_not_called()


def test_export_events_to_otel_starts_background_thread_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("RAG_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://tempo.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer token")
    event = make_trace_event(stage="generator", output_ref={"answer_preview": "ok"})

    with (
        patch("assistant_rh_rag_pipeline.tracing.threading.Thread") as mock_thread,
        patch("assistant_rh_rag_pipeline.tracing.requests.post") as mock_post,
    ):
        export_events_to_otel(turn_id="turn1", trace_id="d" * 32, events=[event], env_label="prod")

    _, kwargs = mock_thread.call_args
    assert kwargs["target"] is _send_otlp_payload
    assert kwargs["args"][0] == "https://tempo.example/v1/traces"
    assert kwargs["daemon"] is True
    mock_thread.return_value.start.assert_called_once()
    mock_post.assert_not_called()


def test_resolve_otlp_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    cases = [
        ("https://tempo.example", "https://tempo.example/v1/traces"),
        ("https://tempo.example/v1/traces", "https://tempo.example/v1/traces"),
        ("https://trace-id.traces.cockpit.fr-par.scw.cloud", "https://trace-id.traces.cockpit.fr-par.scw.cloud/otlp/v1/traces"),
        ("https://trace-id.traces.cockpit.fr-par.scw.cloud/otlp", "https://trace-id.traces.cockpit.fr-par.scw.cloud/otlp/v1/traces"),
        ("https://trace-id.traces.cockpit.fr-par.scw.cloud/otlp/v1/traces", "https://trace-id.traces.cockpit.fr-par.scw.cloud/otlp/v1/traces"),
    ]

    for configured_endpoint, expected_endpoint in cases:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", configured_endpoint)
        assert _resolve_otlp_traces_endpoint() == expected_endpoint


def test_resolve_otlp_endpoint_prefers_explicit_traces_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://trace-id.traces.cockpit.fr-par.scw.cloud")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://custom.example/custom/path")

    assert _resolve_otlp_traces_endpoint() == "https://custom.example/custom/path"


def test_send_otlp_payload_posts(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

    with patch("assistant_rh_rag_pipeline.tracing.requests.post", return_value=_Response()) as mock_post:
        _send_otlp_payload("https://tempo.example/v1/traces", {"resourceSpans": []})

    args, kwargs = mock_post.call_args
    assert args[0] == "https://tempo.example/v1/traces"
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["timeout"] == 3
