from __future__ import annotations

from unittest.mock import patch

from assistant_rh_rag_pipeline.tracing import (
    _build_otlp_payload,
    _resolve_otlp_traces_endpoint,
    _send_otlp_payload,
    bounded_preview,
    export_events_to_otel,
    make_trace_event,
    normalize_trace_id,
)


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
