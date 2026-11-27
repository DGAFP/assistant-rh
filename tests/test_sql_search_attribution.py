"""The attribution experiment must fail closed before it can claim SQL parity."""

import asyncio
import gzip
import json
import zlib
from contextlib import asynccontextmanager

import httpx
import pytest

from scripts.conformance.sql_search_attribution import (
    InferenceTape,
    evaluate_prompt_candidate,
    prompt_variant,
    request_identity,
    served_context_variant,
)

URL = "https://inference.example.invalid/v1/chat/completions"
BODY = {"model": "synthetic", "messages": [{"role": "user", "content": "Frozen source"}], "temperature": 0}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Tests must never call a real inference endpoint")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


def recorded(response="synthetic answer"):
    body = json.dumps(BODY)
    return {
        "key": request_identity("POST", URL, body),
        "method": "POST",
        "url": URL,
        "body": body,
        "status": 200,
        "content_type": "application/json",
        "response": json.dumps({"answer": response}),
        "complete": True,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["prompt", "model", "endpoint", "tape"])
async def test_changed_inference_input_or_altered_tape_is_rejected(mutation):
    row = recorded()
    body = json.loads(json.dumps(BODY))
    url = URL
    if mutation == "prompt":
        body["messages"][0]["content"] += " CHANGED"
    elif mutation == "model":
        body["model"] = "other-model"
    elif mutation == "endpoint":
        url += "/other"
    else:
        row["body"] = row["body"].replace("Frozen source", "Altered source")
    async with httpx.AsyncClient(transport=InferenceTape([row], allowed_hosts={"inference.example.invalid"})) as client:
        with pytest.raises(AssertionError):
            await client.post(url, json=body)


@pytest.mark.anyio
async def test_duplicate_requests_preserve_multiplicity_and_refuse_extra_calls():
    tape = InferenceTape([recorded("first"), recorded("second")], allowed_hosts={"inference.example.invalid"})
    async with httpx.AsyncClient(transport=tape) as client:
        assert (await client.post(URL, json=BODY)).json()["answer"] == "first"
        with pytest.raises(AssertionError, match="Unused"):
            tape.assert_consumed()
        assert (await client.post(URL, json=BODY)).json()["answer"] == "second"
        tape.assert_consumed()
        with pytest.raises(AssertionError, match="extra"):
            await client.post(URL, json=BODY)


@pytest.mark.anyio
async def test_capture_does_not_serialize_authorization_headers():
    tape = InferenceTape(allowed_hosts={"inference.example.invalid"}, allow_live=True)
    await tape.network.aclose()

    def respond(request):
        assert request.headers["authorization"] == "Bearer synthetic-private-key"
        return httpx.Response(200, json={"answer": "synthetic answer"})

    tape.network = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=tape) as client:
        await client.post(URL, json=BODY, headers={"Authorization": "Bearer synthetic-private-key"})
    assert "synthetic-private-key" not in json.dumps(tape.rows)
    replay = InferenceTape(tape.rows, allowed_hosts={"inference.example.invalid"})
    async with httpx.AsyncClient(transport=replay) as client:
        assert (await client.post(URL, json=BODY)).json()["answer"] == "synthetic answer"
    replay.assert_consumed()


@pytest.mark.anyio
async def test_evaluation_refuses_new_provider_destination_before_any_network():
    tape = InferenceTape([])
    async with httpx.AsyncClient(transport=tape) as client:
        with pytest.raises(AssertionError, match="destination"):
            await client.post("https://unapproved.example.invalid/v1/chat/completions", json=BODY)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://albert.api.etalab.gouv.fr/v1",
        "https://albert.api.etalab.gouv.fr/v1?x=1",
        "https://user:synthetic@albert.api.etalab.gouv.fr/v1",
        "https://albert.api.etalab.gouv.fr/v1#fragment",
    ],
)
async def test_evaluation_rejects_noncanonical_provider_urls_before_any_network(url):
    async with httpx.AsyncClient(transport=InferenceTape([])) as client:
        with pytest.raises(AssertionError, match="Unapproved inference URL"):
            await client.post(url, json=BODY)


def test_capture_requires_explicit_live_inference_opt_in():
    with pytest.raises(PermissionError, match="Explicit"):
        InferenceTape()


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_capture_preserves_stream_progress_and_partial_failure(fail):
    state = {"second_read": False, "closed": False}

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            state["second_read"] = True
            if fail:
                raise httpx.ReadError("synthetic mid-stream failure")
            yield b"second"

        async def aclose(self):
            state["closed"] = True

    tape = InferenceTape(allowed_hosts={"inference.example.invalid"}, allow_live=True)
    await tape.network.aclose()
    tape.network = httpx.MockTransport(lambda request: httpx.Response(200, stream=Source()))
    async with httpx.AsyncClient(transport=tape) as client:
        async with client.stream("POST", URL, json=BODY) as response:
            assert not state["second_read"]
            iterator = response.aiter_raw()
            assert await anext(iterator) == b"first"
            assert not state["second_read"]
            if fail:
                with pytest.raises(httpx.ReadError, match="mid-stream"):
                    await anext(iterator)
            else:
                assert await anext(iterator) == b"second"
                with pytest.raises(StopAsyncIteration):
                    await anext(iterator)
    assert state["closed"]
    assert tape.rows[0]["complete"] is not fail
    replay = InferenceTape(tape.rows, allowed_hosts={"inference.example.invalid"})
    async with httpx.AsyncClient(transport=replay) as client:
        if fail:
            with pytest.raises(AssertionError, match="Incomplete"):
                await client.post(URL, json=BODY)
        else:
            async with client.stream("POST", URL, json=BODY) as response:
                assert [chunk async for chunk in response.aiter_raw()] == [b"first", b"second"]
            replay.assert_consumed()


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["done", "missing", "early", "error"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_real_gateway_validates_sse_completion_without_requiring_http_eof(ending, compressed):
    from assistant_rh_api.core.errors import InferenceFailure
    from assistant_rh_api.core.models.inference import ChatRequest, Message, StreamCompleted, TextDelta
    from assistant_rh_api.gateways.chat import ChatGateway
    from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy

    state = {"eof_read": False}

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            encoder = zlib.compressobj(wbits=31)

            def encode(chunk):
                return encoder.compress(chunk) + encoder.flush(zlib.Z_SYNC_FLUSH) if compressed else chunk

            yield encode(b'data: {"choices":[{"delta":{"content":"first"}}]}\r\n\r\n')
            if ending == "error":
                raise httpx.ReadError("synthetic stream failure")
            if ending != "missing":
                yield encode(b"data: [DO")
                yield encode(b"NE]\r\n\r\n")
            state["eof_read"] = True

    tape = InferenceTape(allowed_hosts={"inference.example.invalid"}, allow_live=True)
    await tape.network.aclose()
    headers = {"content-type": "text/event-stream"}
    if compressed:
        headers["content-encoding"] = "gzip"
    tape.network = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=Source()))
    request = ChatRequest((Message("user", "synthetic question"),))
    endpoint = Endpoint("albert", "synthetic", "https://inference.example.invalid/v1", "synthetic-key")
    policy = RequestPolicy(timeout=1, total_timeout=2, max_attempts=1)
    async with httpx.AsyncClient(transport=tape) as client:
        gateway = ChatGateway(client, endpoint, policy=policy)
        async with tape.observe_chat_stream(gateway.stream(request)) as events:
            assert isinstance(await anext(events), TextDelta)
            if ending in {"error", "missing"}:
                with pytest.raises(InferenceFailure) as failure:
                    await anext(events)
                assert failure.value.partial
            elif ending == "done":
                assert isinstance(await anext(events), StreamCompleted)
    assert tape.rows[0]["complete"] is (ending == "done")
    if ending == "done":
        assert not state["eof_read"]
        replay = InferenceTape(tape.rows, allowed_hosts={"inference.example.invalid"})
        async with httpx.AsyncClient(transport=replay) as client:
            gateway = ChatGateway(client, endpoint, policy=policy)
            async with replay.observe_chat_stream(gateway.stream(request)) as events:
                assert [type(event) async for event in events] == [TextDelta, StreamCompleted]
        replay.assert_consumed()


@pytest.mark.anyio
async def test_compressed_json_is_captured_decoded_and_replayed_with_encoding():
    raw = gzip.compress(json.dumps({"answer": "Réponse synthétique"}).encode())

    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw[:8]
            yield raw[8:]

    tape = InferenceTape(allowed_hosts={"inference.example.invalid"}, allow_live=True)
    await tape.network.aclose()
    headers = {"content-type": "application/json", "content-encoding": "gzip"}
    tape.network = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=Source()))
    async with httpx.AsyncClient(transport=tape) as client:
        assert (await client.post(URL, json=BODY)).json()["answer"] == "Réponse synthétique"
    assert json.loads(tape.rows[0]["response"])["answer"] == "Réponse synthétique"
    replay = InferenceTape(tape.rows, allowed_hosts={"inference.example.invalid"})
    async with httpx.AsyncClient(transport=replay) as client:
        assert (await client.post(URL, json=BODY)).json()["answer"] == "Réponse synthétique"
    replay.assert_consumed()


def test_prompt_variant_preserves_complete_context_and_generation_parameters():
    from assistant_rh_api.core.prompt_policy import generation_request

    template = "Synthetic rules for {ministere_sigle}, {today}"
    system = generation_request(template, "", (), "masa", 0, today="2026-10-21").messages[0].content
    body = {
        **BODY,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "Full fixed context"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    variant = prompt_variant(json.dumps(body), template, template + "\nSynthetic candidate rule", "masa", "2026-10-21")
    assert variant["messages"][0]["content"] != body["messages"][0]["content"]
    variant["messages"][0] = body["messages"][0]
    assert variant == body
    with pytest.raises(AssertionError, match="approved baseline"):
        prompt_variant(json.dumps(body), "Other baseline", template, "masa", "2026-10-21")


@pytest.mark.anyio
async def test_prompt_campaign_requires_explicit_consent_before_reading_configuration():
    with pytest.raises(PermissionError, match="Explicit"):
        await evaluate_prompt_candidate({}, lambda value: None)


@pytest.mark.anyio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_prompt_campaign_retains_failed_or_cancelled_partial_trial(monkeypatch, cancelled):
    from assistant_rh_api.core.errors import InferenceFailure
    from assistant_rh_api.core.models.inference import Attempt, TextDelta
    from assistant_rh_api.core.prompt_policy import generation_request
    from assistant_rh_api.gateways.chat import ChatGateway

    @asynccontextmanager
    async def broken_stream(gateway, request):
        async def events():
            yield TextDelta("synthetic partial answer")
            if cancelled:
                raise asyncio.CancelledError()
            raise InferenceFailure((Attempt("albert", "synthetic", "timeout"),), partial=True)

        yield events()

    monkeypatch.setattr(ChatGateway, "stream", broken_stream)
    monkeypatch.setenv("ALBERT_API_KEY", "synthetic-private-key")
    template = "Synthetic baseline"
    system = generation_request(template, "", (), "masa", 0, today="2026-10-21").messages[0].content
    body = {
        "model": "synthetic",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "Synthetic context"}],
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    manifest = {
        "allow_live_inference": True,
        "created_at": "2026-10-21T12:00:00+00:00",
        "baseline_prompt": template,
        "candidate_prompt": template + " Candidate rule",
        "variance_case_ids": [],
        "cases": [
            {
                "id": 1,
                "ministry": "masa",
                "generator_call": {"url": "https://albert.api.etalab.gouv.fr/v1/chat/completions", "body": json.dumps(body)},
            }
        ],
    }
    emitted = []
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await evaluate_prompt_candidate(manifest, emitted.append)
        assert len(emitted) == 1
    else:
        await evaluate_prompt_candidate(manifest, emitted.append)
        assert len(emitted) == 3  # two failed arms plus terminal campaign marker
        assert emitted[0]["partial_failure"] is True
        assert emitted[0]["failure_attempts"][0]["error"] == "timeout"
    assert emitted[0]["answer"] == "synthetic partial answer"
    assert emitted[0]["completed"] is False


def test_served_context_variant_preserves_system_prompt_and_options():
    body = {**BODY, "messages": [{"role": "system", "content": "Fixed rules"}, {"role": "user", "content": "Old context"}]}
    changed = json.loads(json.dumps(body))
    changed["messages"][-1]["content"] = "Candidate context"
    assert served_context_variant(json.dumps(body), json.dumps(changed)) == changed
    changed["model"] = "another-model"
    with pytest.raises(AssertionError, match="generation parameters"):
        served_context_variant(json.dumps(body), json.dumps(changed))
    changed = json.loads(json.dumps(body))
    changed["messages"][0]["content"] = "Other rules"
    with pytest.raises(AssertionError, match="system prompt"):
        served_context_variant(json.dumps(body), json.dumps(changed))
