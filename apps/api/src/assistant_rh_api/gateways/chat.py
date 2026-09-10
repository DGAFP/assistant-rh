"""OpenAI-compatible Albert/Scaleway chat, with pre-content fallback only."""

import math
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any

import httpx

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.inference import Attempt, ChatRequest, Completion, StreamCompleted, TextDelta
from assistant_rh_api.gateways.http import InferenceHTTP, WireFailure, decode_json, invalid
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy, validate_chain


def _choice(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or "error" in data:
        raise invalid()
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise invalid()
    choice = choices[0]
    if choice.get("index", 0) != 0:
        raise invalid()
    reason = choice.get("finish_reason")
    if reason is not None and not isinstance(reason, str):
        raise invalid()
    return choice


def _completion(data: Any) -> tuple[str, str | None]:
    choice = _choice(data)
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise invalid()
    return message["content"].strip(), choice.get("finish_reason")


async def _sse(http: InferenceHTTP, response: httpx.Response, deadline: float) -> AsyncGenerator[str]:
    """SSE fields, comments, multiline data and CRLF across arbitrary chunks."""
    buffer = b""
    fields: list[str] = []
    async for chunk in http.chunks(response, deadline):
        buffer += chunk
        while b"\n" in buffer:
            http.remaining(deadline)
            raw, buffer = buffer.split(b"\n", 1)
            try:
                line = raw.removesuffix(b"\r").decode("utf-8")
            except UnicodeError:
                raise invalid() from None
            if not line:
                if fields:
                    yield "\n".join(fields)
                    fields = []
            elif line.startswith("data:"):
                fields.append(line[5:].removeprefix(" "))
    # EOF never substitutes for the provider's [DONE] marker.
    raise invalid()


class ChatGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        primary: Endpoint,
        fallback: Endpoint | None = None,
        *,
        policy: RequestPolicy = RequestPolicy(timeout=120, total_timeout=240),
    ) -> None:
        validate_chain(primary, fallback)
        self._endpoints = (primary,) if fallback is None else (primary, fallback)
        self._http = InferenceHTTP(client, policy)

    @staticmethod
    def _payload(request: ChatRequest, endpoint: Endpoint, *, stream: bool) -> dict[str, Any]:
        if (
            not request.messages
            or not math.isfinite(request.temperature)
            or not 0 <= request.temperature <= 2
            or any(message.role not in ("system", "user", "assistant") or not isinstance(message.content, str) for message in request.messages)
        ):
            raise ValueError("invalid chat request")
        return {
            "model": endpoint.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "stream": stream,
        }

    async def complete(self, request: ChatRequest) -> Completion:
        attempts: list[Attempt] = []
        for endpoint in self._endpoints:
            try:
                text, reason = await self._http.json(
                    endpoint,
                    "/chat/completions",
                    self._payload(request, endpoint, stream=False),
                    _completion,
                    attempts,
                    self._http.deadline(),
                )
                return Completion(text, endpoint.provider, endpoint.model, tuple(attempts), reason)
            except InferenceFailure:
                continue
        raise InferenceFailure(tuple(attempts)) from None

    @asynccontextmanager
    async def stream(self, request: ChatRequest) -> AsyncIterator[AsyncIterator[TextDelta | StreamCompleted]]:
        async with aclosing(self._stream(request)) as events:
            yield events

    async def _stream(self, request: ChatRequest) -> AsyncGenerator[TextDelta | StreamCompleted]:
        attempts: list[Attempt] = []
        emitted = False
        for endpoint in self._endpoints:
            payload = self._payload(request, endpoint, stream=True)
            deadline = self._http.deadline()
            for number in range(1, self._http.policy.max_attempts + 1):
                reason = None
                seen_choice = False
                try:
                    async with self._http.open(endpoint, "/chat/completions", payload, deadline) as response:
                        async with aclosing(_sse(self._http, response, deadline)) as events:
                            async for event in events:
                                if event == "[DONE]":
                                    if not seen_choice:
                                        raise invalid()
                                    attempts.append(Attempt(endpoint.provider, endpoint.model))
                                    break
                                data = decode_json(event)
                                if (
                                    isinstance(data, dict)
                                    and "error" not in data
                                    and data.get("choices") == []
                                    and isinstance(data.get("usage"), dict)
                                ):
                                    continue
                                choice = _choice(data)
                                seen_choice = True
                                delta = choice.get("delta")
                                if not isinstance(delta, dict):
                                    raise invalid()
                                content = delta.get("content")
                                if content is not None and not isinstance(content, str):
                                    raise invalid()
                                if reason is not None and content:
                                    raise invalid()
                                reason = choice.get("finish_reason") or reason
                                if content:
                                    emitted = True
                                    yield TextDelta(content)
                    # Close HTTP before exposing the terminal outcome.
                    yield StreamCompleted(endpoint.provider, endpoint.model, tuple(attempts), reason)
                    return
                except WireFailure as exc:
                    attempts.append(exc.attempt(endpoint))
                    if emitted:
                        raise InferenceFailure(tuple(attempts), partial=True) from None
                    if not await self._http.retry(exc, number, deadline):
                        break
        raise InferenceFailure(tuple(attempts)) from None
