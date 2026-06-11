"""
Contract tests for the Albert /rerank payload (issue #87).

The Albert API expects ``query`` / ``documents`` (the legacy
``prompt`` / ``input`` schema returns HTTP 422).  These tests mock the HTTP
layer and assert the exact payload schema, the response parsing, and the
fallback behaviour of ``maybe_rerank``.

Usage:
    pytest tests/test_reranker_contract.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from assistant_rh_rag_pipeline.reranker import AlbertReranker, maybe_rerank


def _make_reranker(response_json=None, raise_for_status=None):
    """Build an AlbertReranker whose HTTP POST is mocked.

    Returns ``(reranker, mock_post)`` so tests can inspect the call.
    """
    reranker = AlbertReranker(
        model="openweight-rerank",
        base_url="https://albert.example/v1",
        api_key="test-key",
    )
    response = MagicMock()
    response.json.return_value = response_json or {"data": []}
    if raise_for_status is not None:
        response.raise_for_status.side_effect = raise_for_status
    mock_post = MagicMock(return_value=response)
    reranker._post = mock_post
    return reranker, mock_post


class TestRerankPayloadContract:
    def test_payload_uses_query_and_documents(self):
        reranker, mock_post = _make_reranker()
        reranker.rerank("droits RTT", ["doc a", "doc b"], top_k=2)

        payload = mock_post.call_args.kwargs["json"]
        assert payload == {
            "model": "openweight-rerank",
            "query": "droits RTT",
            "documents": ["doc a", "doc b"],
            "top_n": 2,
        }

    def test_payload_has_no_legacy_fields(self):
        reranker, mock_post = _make_reranker()
        reranker.rerank("q", ["d1"])

        payload = mock_post.call_args.kwargs["json"]
        assert "prompt" not in payload
        assert "input" not in payload

    def test_endpoint_and_auth_header(self):
        reranker, mock_post = _make_reranker()
        reranker.rerank("q", ["d1"])

        url = mock_post.call_args.args[0]
        assert url == "https://albert.example/v1/rerank"
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-key"

    def test_top_n_defaults_to_document_count(self):
        reranker, mock_post = _make_reranker()
        reranker.rerank("q", ["d1", "d2", "d3"])

        assert mock_post.call_args.kwargs["json"]["top_n"] == 3

    def test_empty_documents_skip_http_call(self):
        reranker, mock_post = _make_reranker()
        assert reranker.rerank("q", []) == []
        mock_post.assert_not_called()


class TestRerankResponseParsing:
    def test_parses_data_with_relevance_score(self):
        reranker, _ = _make_reranker(
            {
                "data": [
                    {"index": 1, "relevance_score": 0.92},
                    {"index": 0, "relevance_score": 0.13},
                ]
            }
        )
        assert reranker.rerank("q", ["d1", "d2"]) == [(1, 0.92), (0, 0.13)]

    def test_parses_results_fallback_with_score(self):
        reranker, _ = _make_reranker({"results": [{"index": 0, "score": 0.5}]})
        assert reranker.rerank("q", ["d1"]) == [(0, 0.5)]

    def test_http_error_propagates(self):
        reranker, _ = _make_reranker(
            raise_for_status=Exception("422 Unprocessable Entity"),
        )
        with pytest.raises(Exception, match="422"):
            reranker.rerank("q", ["d1"])


class TestMaybeRerankFallback:
    def test_failure_returns_identity_order(self, monkeypatch, caplog):
        import assistant_rh_rag_pipeline.reranker as reranker_mod

        class _Boom:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("API unreachable")

        monkeypatch.setattr(reranker_mod, "AlbertReranker", _Boom)
        with caplog.at_level("ERROR", logger="assistant_rh_rag_pipeline.reranker"):
            ranking = maybe_rerank("q", ["d1", "d2"], enabled=True)

        assert [idx for idx, _ in ranking] == [0, 1]
        assert any("Reranking failed" in rec.message for rec in caplog.records)

    def test_disabled_returns_identity_order(self):
        ranking = maybe_rerank("q", ["d1", "d2", "d3"], enabled=False)
        assert [idx for idx, _ in ranking] == [0, 1, 2]
