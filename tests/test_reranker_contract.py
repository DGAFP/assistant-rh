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


def test_rerank_standard_input_is_single_request() -> None:
    """La config standard (v3_rerank_input_k=40) tient en UNE requête /rerank
    (validé contre Albert, revue #335) : aucune dérive inter-requêtes possible
    sur le chemin nominal."""
    from assistant_rh_rag_pipeline.reranker import AlbertReranker

    calls: list[dict] = []

    class _Resp:
        def __init__(self, n: int):
            self._n = n

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": [{"index": i, "score": 1.0 - i * 0.01} for i in range(self._n)]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(len(json["documents"]))

    reranker = AlbertReranker(api_key="k")
    reranker._post = fake_post
    ranked = reranker.rerank("question", [f"doc {i}" for i in range(40)], top_k=20)
    assert len(calls) == 1
    assert len(calls[0]["documents"]) == 40
    assert calls[0]["top_n"] == 40  # jamais tronqué avant la sélection finale
    assert len(ranked) == 20


def test_rerank_beyond_batch_size_merges_with_offsets(monkeypatch) -> None:
    """Au-delà de _BATCH_SIZE : fusion multi-lots — mécanique vérifiée
    (offsets d'indices, top_n = lot entier, troncature top_k après fusion).
    La fusion inter-requêtes est APPROXIMATIVE en conditions réelles (dérive
    des scores Albert ~6e-4 mesurée, revue #335) : ce test valide la
    mécanique, pas une exactitude numérique que l'API ne garantit pas."""
    from assistant_rh_rag_pipeline.reranker import AlbertReranker

    calls: list[dict] = []

    class _Resp:
        def __init__(self, n: int, base: float):
            self._n = n
            self._base = base

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": [{"index": i, "score": self._base - i * 0.01} for i in range(self._n)]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(len(json["documents"]), 0.99 if len(calls) == 2 else 0.90)

    reranker = AlbertReranker(api_key="k")
    reranker._post = fake_post
    ranked = reranker.rerank("question", [f"doc {i}" for i in range(80)], top_k=20)

    assert len(calls) == 2
    assert [c["top_n"] for c in calls] == [40, 40]
    assert len(ranked) == 20
    # offsets appliqués : le meilleur global vient du 2e lot (index original 40)
    assert ranked[0] == (40, 0.99)
    assert any(idx < 40 for idx, _ in ranked) and any(idx >= 40 for idx, _ in ranked)


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
