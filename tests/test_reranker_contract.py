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


def test_rerank_batches_and_merges_exactly(monkeypatch) -> None:
    """P1 (vague 1) : entrée 40 > _BATCH_SIZE -> 2 requêtes /rerank, fusion par
    tri global des scores (exact : scores cross-encoder indépendants par doc),
    troncature top_k APRÈS fusion."""
    from assistant_rh_rag_pipeline.reranker import AlbertReranker

    calls: list[dict] = []

    class _Resp:
        def __init__(self, n: int, offset_score: float):
            self._n = n
            self._offset_score = offset_score

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            # scores décroissants dans chaque lot ; le 2e lot contient le
            # meilleur score global (0.99) -> il doit sortir en tête.
            return {"data": [{"index": i, "score": self._offset_score - i * 0.01} for i in range(self._n)]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(len(json["documents"]), 0.99 if len(calls) == 2 else 0.90)

    reranker = AlbertReranker(api_key="k")
    reranker._post = fake_post
    texts = [f"doc {i}" for i in range(40)]
    ranked = reranker.rerank("question", texts, top_k=20)

    assert len(calls) == 2
    assert [len(c["documents"]) for c in calls] == [20, 20]
    # top_n par lot = tout le lot (jamais tronqué avant fusion)
    assert [c["top_n"] for c in calls] == [20, 20]
    assert len(ranked) == 20
    # le meilleur global vient du 2e lot : index original 20 (offset appliqué)
    assert ranked[0] == (20, 0.99)
    # indices originaux des deux lots présents dans la fusion
    assert any(idx < 20 for idx, _ in ranked) and any(idx >= 20 for idx, _ in ranked)


def test_aggregator_rerank_input_follows_config(monkeypatch) -> None:
    """L'entrée du reranker est pilotée par config.rerank_input_k (plus de
    constante en dur) et n'est jamais inférieure à la sortie."""
    from assistant_rh_rag_pipeline.config import SectionAggregationConfig
    from assistant_rh_rag_pipeline.models import AggregatedSection
    from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

    cfg = SectionAggregationConfig(section_rerank_top_k=20, rerank_input_k=40)
    agg = SectionAggregator(cfg, dsn="postgresql://fake")

    seen: dict = {}

    class _FakeReranker:
        def rerank(self, query, texts, top_k=None):
            seen["n_texts"] = len(texts)
            seen["top_k"] = top_k
            return [(i, 1.0 - i * 0.01) for i in range(min(top_k or len(texts), len(texts)))]

    agg._reranker = _FakeReranker()
    sections = [
        AggregatedSection(section_id=str(i), heading=f"h{i}", markdown=f"m{i}", score=1.0 - i * 0.001, chunks=[])
        for i in range(60)
    ]
    out, status, err = agg._rerank("q", sections)
    assert status == "completed"
    assert seen["n_texts"] == 40  # entrée élargie à rerank_input_k
    assert seen["top_k"] == 20  # sortie inchangée
    assert len(out) == 20

    # jamais moins que la sortie, même si rerank_input_k est mal réglé plus bas
    cfg2 = SectionAggregationConfig(section_rerank_top_k=20, rerank_input_k=5)
    agg2 = SectionAggregator(cfg2, dsn="postgresql://fake")
    agg2._reranker = _FakeReranker()
    agg2._rerank("q", sections)
    assert seen["n_texts"] == 20
