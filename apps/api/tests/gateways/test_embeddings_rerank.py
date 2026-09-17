import asyncio
import json
import math
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.inference import RankedDocument
from assistant_rh_api.core.ports.inference import EmbeddingPort, RerankerPort
from assistant_rh_api.gateways.embeddings import EmbeddingCircuit, EmbeddingGateway
from assistant_rh_api.gateways.rerank import RerankerGateway

from .conftest import ALBERT, POLICY, SCALEWAY, WireStream

pytestmark = pytest.mark.anyio
EMBED_ALBERT = replace(ALBERT, model="openweight-embeddings")
EMBED_SCALEWAY = replace(SCALEWAY, model="bge-multilingual-gemma2")
RERANK = replace(ALBERT, model="openweight-rerank")


class Clock:
    value = 100.0

    def monotonic(self):
        return self.value

    def now(self):
        return datetime(2026, 9, 8, tzinfo=UTC)


def embedding(dimensions):
    return {"data": [{"index": 0, "embedding": [3, 4] + [0] * (dimensions - 2)}]}


@pytest.mark.parametrize("fallback", [False, True])
async def test_embedding_model_identity_dimensions_and_normalization(fallback):
    def handle(request):
        assert request.url.path == "/v1/embeddings"
        payload = json.loads(request.content)
        assert payload["input"] == "query"
        if request.url.host == "albert.test":
            assert payload["model"] == EMBED_ALBERT.model
            return httpx.Response(503) if fallback else httpx.Response(200, json=embedding(1024))
        assert payload["model"] == EMBED_SCALEWAY.model
        return httpx.Response(200, json=embedding(3584))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway: EmbeddingPort = EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY)
        result = await gateway.embed("query")
    assert result.model == ("bge_scaleway" if fallback else "albert")
    assert result.provider == ("scaleway" if fallback else "albert")
    assert len(result.vector) == (3584 if fallback else 1024)
    assert result.vector[:2] == (0.6, 0.8) and math.hypot(*result.vector) == 1
    assert len(result.attempts) == (2 if fallback else 1)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"data": []},
        {"data": [{"embedding": [1, 2]}]},
        {"data": [{"embedding": [0] * 1024}]},
        {"data": [{"embedding": [True] * 1024}]},
        {"data": [{"embedding": ["1"] * 1024}]},
        {"data": [{"embedding": [float("nan")] * 1024}]},
        {"data": [{"embedding": [float("inf")] * 1024}]},
    ],
)
async def test_invalid_vector_is_typed_and_falls_back(bad):
    def handle(request):
        data = bad if request.url.host == "albert.test" else embedding(3584)
        return httpx.Response(200, content=json.dumps(data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY).embed("query")
    assert result.model == "bge_scaleway" and result.attempts[0].error == "invalid_response"


async def test_embedding_double_failure_is_explicit_for_lexical_policy():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(InferenceFailure) as caught:
            await EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY).embed("query")
    assert [a.provider for a in caught.value.attempts] == ["albert", "scaleway"]


async def test_embedding_cooldown_expiry_and_no_cross_instance_state():
    clock = Clock()
    circuit = EmbeddingCircuit(clock)
    calls = []
    broken = True

    def handle(request):
        calls.append(request.url.host)
        if request.url.host == "albert.test":
            return httpx.Response(503) if broken else httpx.Response(200, json=embedding(1024))
        return httpx.Response(200, json=embedding(3584))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY, circuit=circuit)
        first = await gateway.embed("query")
        second = await gateway.embed("query")
        assert second.attempts[0].error == "circuit_open"
        isolated = EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY, circuit=EmbeddingCircuit(clock))
        await isolated.embed("query")
        clock.value += 60
        broken = False
        recovered = await gateway.embed("query")
    assert first.model == "bge_scaleway" and recovered.model == "albert"
    assert calls == ["albert.test", "scaleway.test", "scaleway.test", "albert.test", "scaleway.test", "albert.test"]


async def test_concurrent_embedding_results_and_old_success_cannot_clear_new_failure():
    clock = Clock()
    circuit = EmbeddingCircuit(clock)
    primary_started = asyncio.Event()
    fallback_done = asyncio.Event()

    async def handle(request):
        text = json.loads(request.content)["input"]
        if request.url.host == "albert.test":
            if text == "primary":
                primary_started.set()
                await fallback_done.wait()
                return httpx.Response(200, json=embedding(1024))
            await primary_started.wait()
            return httpx.Response(503)
        fallback_done.set()
        return httpx.Response(200, json=embedding(3584))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY, circuit=circuit)
        primary, backup = await asyncio.gather(gateway.embed("primary"), gateway.embed("backup"))
    assert (primary.model, len(primary.vector), len(primary.attempts)) == ("albert", 1024, 1)
    assert (backup.model, len(backup.vector), len(backup.attempts)) == ("bge_scaleway", 3584, 2)
    assert circuit.acquire() is None


async def test_embedding_cancellation_does_not_open_circuit_or_call_fallback():
    body = WireStream(wait_after=True)
    circuit = EmbeddingCircuit(Clock())
    calls = []

    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=POLICY, circuit=circuit)
        task = asyncio.create_task(gateway.embed("query"))
        await body.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == ["albert.test"] and circuit.acquire() is not None and body.closed


@pytest.mark.parametrize("response_key,score_key", [("data", "relevance_score"), ("results", "score")])
async def test_rerank_batches_all_candidates_then_global_order(response_key, score_key):
    batches = []

    def handle(request):
        assert request.url.path == "/v1/rerank"
        payload = json.loads(request.content)
        docs = payload["documents"]
        batches.append(len(docs))
        assert payload["top_n"] == len(docs) and payload["query"] == "query" and payload["model"] == RERANK.model
        return httpx.Response(200, json={response_key: [{"index": i, score_key: int(doc) % 3} for i, doc in reversed(list(enumerate(docs)))]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway: RerankerPort = RerankerGateway(client, RERANK, policy=POLICY)
        result = await gateway.rerank("query", tuple(map(str, range(81))), top_k=30)
    expected = sorted(range(81), key=lambda i: (-(i % 3), i))[:30]
    assert [doc.index for doc in result.documents] == expected
    assert batches == [40, 40, 1] and len(result.attempts) == 3 and not result.fallback


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"data": []},
        {"data": [{"index": -1, "score": 1}, {"index": 1, "score": 1}]},
        {"data": [{"index": 0, "score": 1}, {"index": 0, "score": 1}]},
        {"data": [{"index": True, "score": 1}, {"index": 1, "score": 1}]},
        {"data": [{"index": 0, "score": float("nan")}, {"index": 1, "score": 1}]},
        {"data": [{"index": 0}, {"index": 1, "score": 1}]},
    ],
)
async def test_invalid_rerank_has_explicit_input_order_fallback(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(data)))) as client:
        result = await RerankerGateway(client, RERANK, policy=POLICY).rerank("q", ("a", "b"))
    assert result.fallback and result.attempts[-1].error == "invalid_response"
    assert result.documents == (RankedDocument(0, 1), RankedDocument(1, 0.999))


async def test_zero_score_is_not_replaced_by_alternate_score():
    rows = [{"index": 0, "relevance_score": 0, "score": 100}, {"index": 1, "score": 1}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": rows}))) as client:
        result = await RerankerGateway(client, RERANK, policy=POLICY).rerank("q", ("a", "b"))
    assert result.documents == (RankedDocument(1, 1), RankedDocument(0, 0))


@pytest.mark.parametrize("strict", [False, True])
async def test_later_batch_failure_discards_all_partial_ranking(strict):
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"index": i, "score": i} for i in range(40)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = RerankerGateway(client, RERANK, policy=POLICY, fallback_on_error=not strict)
        if strict:
            with pytest.raises(InferenceFailure) as caught:
                await gateway.rerank("q", tuple(map(str, range(41))), top_k=2)
            assert len(caught.value.attempts) == 2
        else:
            result = await gateway.rerank("q", tuple(map(str, range(41))), top_k=2)
            assert result.fallback and [doc.index for doc in result.documents] == [0, 1]
            assert [a.error for a in result.attempts] == [None, "unavailable"]


async def test_rerank_empty_and_zero_limit_do_not_contact_provider():
    def handle(request):
        raise AssertionError("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = RerankerGateway(client, RERANK, policy=POLICY)
        for documents, top_k in [((), None), (("a",), 0)]:
            result = await gateway.rerank("q", documents, top_k=top_k)
            assert not result.documents and not result.attempts and not result.fallback


async def test_rerank_cancellation_is_not_a_successful_fallback():
    body = WireStream(wait_after=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        task = asyncio.create_task(RerankerGateway(client, RERANK, policy=POLICY).rerank("q", ("a",)))
        await body.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.closed


async def test_rerank_timeout_is_explicit_fallback():
    body = WireStream(wait_after=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = RerankerGateway(client, RERANK, policy=replace(POLICY, timeout=0.01))
        result = await gateway.rerank("q", ("a",))
    assert result.fallback and result.attempts[0].error == "timeout" and body.closed


async def test_embedding_timeout_falls_back_with_correct_vector_identity():
    body = WireStream(wait_after=True)

    def handle(request):
        if request.url.host == "albert.test":
            return httpx.Response(200, stream=body)
        return httpx.Response(200, json=embedding(3584))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await EmbeddingGateway(client, EMBED_ALBERT, EMBED_SCALEWAY, policy=replace(POLICY, timeout=0.01)).embed("q")
    assert result.attempts[0].error == "timeout" and result.model == "bge_scaleway" and len(result.vector) == 3584 and body.closed
