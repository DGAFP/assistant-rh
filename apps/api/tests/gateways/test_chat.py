import asyncio
import json
from dataclasses import FrozenInstanceError, replace

import anyio
import httpx
import pytest
from assistant_rh_api.core.inference import ChatRequest, InferenceFailure, Message, StreamCompleted, TextDelta
from assistant_rh_api.core.ports.inference import LLMPort
from assistant_rh_api.gateways.chat import ChatGateway
from assistant_rh_api.gateways.settings import RequestPolicy

from .conftest import ALBERT, POLICY, SCALEWAY, WireStream

pytestmark = pytest.mark.anyio
REQUEST = ChatRequest((Message("system", "rules"), Message("user", "question")))


def reply(text="answer"):
    return {"choices": [{"index": 0, "message": {"content": text}, "finish_reason": "stop"}]}


def event(content=None, *, reason=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": reason}]}) + "\n\n").encode()


async def test_complete_payload_and_immutable_outcome():
    def handle(request):
        assert request.url == "https://albert.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-albert-key"
        assert json.loads(request.content) == {
            "model": ALBERT.model,
            "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}],
            "temperature": 0,
            "stream": False,
        }
        assert request.extensions["timeout"]["read"] <= POLICY.timeout
        return httpx.Response(200, json=reply(" answer "))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway: LLMPort = ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY)
        result = await gateway.complete(REQUEST)
        assert (result.text, result.provider, result.model, result.finish_reason) == ("answer", "albert", ALBERT.model, "stop")
        assert len(result.attempts) == 1 and result.attempts[0].error is None
        with pytest.raises(FrozenInstanceError):
            result.text = "changed"
        assert not client.is_closed


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(401, text="private provider body"), "rejected"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(503), "unavailable"),
        (httpx.Response(408), "timeout"),
        (httpx.Response(200, json={}), "invalid_response"),
        (httpx.Response(200, text="not json"), "invalid_response"),
        (httpx.Response(200, json=reply(None)), "invalid_response"),
        (httpx.Response(200, json={"error": "secret", **reply()}), "invalid_response"),
    ],
)
async def test_primary_failure_uses_scaleway(response, error):
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        return response if request.url.host == "albert.test" else httpx.Response(200, json=reply("backup"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY).complete(REQUEST)
    assert hosts == ["albert.test", "scaleway.test"]
    assert result.provider == "scaleway" and result.text == "backup"
    assert [a.error for a in result.attempts] == [error, None]


async def test_double_failure_safe_error_and_no_chained_provider_exception():
    def handle(request):
        raise httpx.ConnectError("private-url-and-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(InferenceFailure) as caught:
            await ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY).complete(REQUEST)
    assert str(caught.value) == "inference_failure"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert [a.error for a in caught.value.attempts] == ["unavailable", "unavailable"]
    assert not caught.value.partial


@pytest.mark.parametrize("status,calls", [(503, 3), (429, 3), (401, 1), (400, 1), (302, 1)])
async def test_bounded_retries_and_no_redirects(status, calls):
    seen = []

    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(status, headers={"location": "https://untrusted.test/", "retry-after": "999999"})

    policy = replace(POLICY, max_attempts=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(InferenceFailure) as caught:
            await ChatGateway(client, ALBERT, policy=policy).complete(REQUEST)
    assert seen == ["albert.test"] * calls
    assert len(caught.value.attempts) == calls


async def test_retry_success_keeps_attempt_history():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, json=reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await ChatGateway(client, ALBERT, SCALEWAY, policy=replace(POLICY, max_attempts=2)).complete(REQUEST)
    assert result.provider == "albert" and [a.error for a in result.attempts] == ["unavailable", None]


async def test_actual_timeout_before_headers_falls_back():
    async def handle(request):
        if request.url.host == "albert.test":
            await asyncio.Event().wait()
        return httpx.Response(200, json=reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with asyncio.timeout(1):
            result = await ChatGateway(client, ALBERT, SCALEWAY, policy=replace(POLICY, timeout=0.01)).complete(REQUEST)
    assert result.attempts[0].error == "timeout"


async def test_timeout_during_body_closes_response():
    body = WireStream(b'{"choices":', wait_after=True)

    def handle(request):
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(InferenceFailure) as caught:
            await ChatGateway(client, ALBERT, policy=replace(POLICY, timeout=0.01)).complete(REQUEST)
    assert caught.value.attempts[0].error == "timeout" and body.closed


async def test_concurrent_calls_keep_individual_diagnostics():
    both_started = asyncio.Event()
    count = 0

    async def handle(request):
        nonlocal count
        text = json.loads(request.content)["messages"][0]["content"]
        if request.url.host == "albert.test":
            count += 1
            if count == 2:
                both_started.set()
            await both_started.wait()
            if text == "fallback":
                return httpx.Response(503)
        return httpx.Response(200, json=reply(text))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY)
        a, b = await asyncio.gather(*(gateway.complete(ChatRequest((Message("user", text),))) for text in ("primary", "fallback")))
    assert (a.text, a.provider, len(a.attempts)) == ("primary", "albert", 1)
    assert (b.text, b.provider, len(b.attempts)) == ("fallback", "scaleway", 2)


async def test_stream_comments_split_unicode_usage_and_terminal_closes():
    raw = b": ping\r\n\r\n" + event("é").replace(b"\\u00e9", "é".encode()) + event(reason="stop")
    raw += b'data: {"choices": [], "usage": {"total_tokens": 1}}\r\n\r\ndata: [DONE]\r\n\r\n'
    body = WireStream(*(bytes([byte]) for byte in raw))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        async with ChatGateway(client, ALBERT, policy=POLICY).stream(REQUEST) as stream:
            events = [part async for part in stream]
    assert events[0] == TextDelta("é")
    assert isinstance(events[1], StreamCompleted) and events[1].finish_reason == "stop"
    assert len(events) == 2 and body.closed


@pytest.mark.parametrize(
    "chunks",
    [
        (event(None), httpx.ReadError("secret")),
        (event(""), httpx.ReadError("secret")),
        (b"data: {bad}\n\n",),
        (b'data: {"error": "secret"}\n\n',),
        (b"data: [DONE]\n\n",),
    ],
)
async def test_stream_pre_content_failure_falls_back(chunks):
    first = WireStream(*chunks)
    second = WireStream(event("backup"), event(reason="stop"), b"data: [DONE]\n\n")

    def handle(request):
        return httpx.Response(200, stream=first if request.url.host == "albert.test" else second)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY).stream(REQUEST) as stream:
            events = [part async for part in stream]
    assert events[0] == TextDelta("backup") and events[-1].provider == "scaleway"
    assert len(events[-1].attempts) == 2 and first.closed and second.closed


@pytest.mark.parametrize("tail", [(), (httpx.ReadError("secret"),), (b"data: broken\n\n",)])
async def test_stream_partial_failure_never_retries_or_falls_back(tail):
    body = WireStream(event("partial"), *tail)
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with ChatGateway(client, ALBERT, SCALEWAY, policy=replace(POLICY, max_attempts=3)).stream(REQUEST) as stream:
            assert await anext(stream) == TextDelta("partial")
            with pytest.raises(InferenceFailure) as caught:
                await anext(stream)
    assert caught.value.partial and hosts == ["albert.test"] and body.closed


async def test_stream_early_break_closes_immediately():
    body = WireStream(event("one"), wait_after=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        async with ChatGateway(client, ALBERT, policy=POLICY).stream(REQUEST) as stream:
            async for _ in stream:
                break
        assert body.closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_closes_body_and_never_falls_back(streaming):
    body = WireStream(wait_after=True)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY)

        async def run():
            if streaming:
                async with gateway.stream(REQUEST) as stream:
                    return [part async for part in stream]
            return await gateway.complete(REQUEST)

        task = asyncio.create_task(run())
        await body.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1 and body.closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_asgi_level_cancellation_shields_awaited_cleanup(streaming):
    class AsyncClose(WireStream):
        async def aclose(self):
            await anyio.sleep(0)
            self.closed = True

    body = AsyncClose(wait_after=True)
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY)
        with anyio.move_on_after(0.01) as scope:
            if streaming:
                async with gateway.stream(REQUEST) as stream:
                    await anext(stream)
            else:
                await gateway.complete(REQUEST)
        assert scope.cancel_called
    assert hosts == ["albert.test"] and body.closed


async def test_cleanup_that_never_finishes_is_bounded():
    class StuckClose(WireStream):
        async def aclose(self):
            await asyncio.Event().wait()

    body = StuckClose(json.dumps(reply()).encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=replace(POLICY, timeout=0.01))
        async with asyncio.timeout(1):
            # HTTPX closes at EOF as well; that close must respect the read budget.
            with pytest.raises(InferenceFailure) as caught:
                await gateway.complete(REQUEST)
    assert caught.value.attempts[-1].error == "timeout"


async def test_total_stream_deadline_includes_trickled_chunks():
    class Trickle(WireStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield event(".")

    body = Trickle()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=replace(POLICY, total_timeout=0.03))
        async with gateway.stream(REQUEST) as stream:
            async with asyncio.timeout(1):
                with pytest.raises(InferenceFailure) as caught:
                    async for _ in stream:
                        pass
    assert caught.value.partial and caught.value.attempts[-1].error == "timeout" and body.closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_response_size_is_bounded(streaming):
    body = WireStream(b"x" * 20)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=replace(POLICY, max_response_bytes=10))
        with pytest.raises(InferenceFailure) as caught:
            if streaming:
                async with gateway.stream(REQUEST) as stream:
                    await anext(stream)
            else:
                await gateway.complete(REQUEST)
    assert caught.value.attempts[-1].error == "invalid_response" and body.closed


@pytest.mark.parametrize("buffered", [False, True])
async def test_caller_is_not_cancelled_while_processing_a_token(buffered):
    chunks = (event("one"), b"data: [DONE]\n\n")
    body = WireStream(*((b"".join(chunks),) if buffered else chunks))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        gateway = ChatGateway(client, ALBERT, policy=replace(POLICY, total_timeout=0.01))
        async with gateway.stream(REQUEST) as stream:
            assert await anext(stream) == TextDelta("one")
            await asyncio.sleep(0.02)  # Deadline must not cancel the consumer's own work.
            with pytest.raises(InferenceFailure) as caught:
                await anext(stream)
    assert caught.value.partial and caught.value.attempts[-1].error == "timeout"


async def test_stream_double_failure_has_both_attempts():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        async with ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY).stream(REQUEST) as stream:
            with pytest.raises(InferenceFailure) as caught:
                await anext(stream)
    assert not caught.value.partial and [a.provider for a in caught.value.attempts] == ["albert", "scaleway"]


async def test_concurrent_streams_keep_tokens_and_terminal_diagnostics_separate():
    started = asyncio.Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        text = json.loads(request.content)["messages"][0]["content"]
        if request.url.host == "albert.test":
            calls += 1
            if calls == 2:
                started.set()
            await started.wait()
            if text == "backup":
                return httpx.Response(503)
        return httpx.Response(200, stream=WireStream(event(text), b"data: [DONE]\n\n"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = ChatGateway(client, ALBERT, SCALEWAY, policy=POLICY)

        async def collect(text):
            async with gateway.stream(ChatRequest((Message("user", text),))) as stream:
                return [part async for part in stream]

        primary, backup = await asyncio.gather(collect("primary"), collect("backup"))
    assert primary[0] == TextDelta("primary") and primary[-1].provider == "albert" and len(primary[-1].attempts) == 1
    assert backup[0] == TextDelta("backup") and backup[-1].provider == "scaleway" and len(backup[-1].attempts) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout", 0),
        ("timeout", float("nan")),
        ("total_timeout", -1),
        ("max_attempts", 4),
        ("max_attempts", True),
        ("retry_delay", -1),
        ("max_response_bytes", 0),
    ],
)
async def test_invalid_policy(field, value):
    with pytest.raises(ValueError):
        RequestPolicy(**{field: value})


async def test_endpoint_credentials_hidden_and_fallback_order_validated():
    assert "private" not in repr(ALBERT) and "https" not in repr(ALBERT)
    with pytest.raises(ValueError):
        replace(ALBERT, base_url="https://user:secret@albert.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with pytest.raises(ValueError):
            ChatGateway(client, SCALEWAY, ALBERT)
