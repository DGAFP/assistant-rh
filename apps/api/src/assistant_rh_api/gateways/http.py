"""Bounded HTTP I/O and retry policy shared by inference adapters."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import anyio
import httpx

from assistant_rh_api.core.inference import Attempt, FailureKind, InferenceFailure
from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy

T = TypeVar("T")


class WireFailure(Exception):
    def __init__(self, kind: FailureKind, status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status

    def attempt(self, endpoint: Endpoint) -> Attempt:
        return Attempt(endpoint.provider, endpoint.model, self.kind, self.status)


def invalid() -> WireFailure:
    return WireFailure("invalid_response")


def decode_json(raw: bytes | str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise invalid() from None


class InferenceHTTP:
    """The caller owns the injected AsyncClient. No global clients or SDK retries."""

    def __init__(self, client: httpx.AsyncClient, policy: RequestPolicy) -> None:
        self.client = client
        self.policy = policy

    def deadline(self) -> float:
        return asyncio.get_running_loop().time() + self.policy.total_timeout

    def remaining(self, deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise WireFailure("timeout")
        return min(remaining, self.policy.timeout)

    @asynccontextmanager
    async def open(self, endpoint: Endpoint, path: str, payload: dict[str, Any], deadline: float) -> AsyncIterator[httpx.Response]:
        response = None
        try:
            request = self.client.build_request(
                "POST",
                endpoint.base_url.rstrip("/") + path,
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json=payload,
                timeout=self.remaining(deadline),
            )
            async with asyncio.timeout(self.remaining(deadline)):
                response = await self.client.send(request, stream=True, follow_redirects=False)
            status = response.status_code
            if status == 429:
                raise WireFailure("rate_limited", status)
            if status in (408, 504):
                raise WireFailure("timeout", status)
            if status >= 500:
                raise WireFailure("unavailable", status)
            if not 200 <= status < 300:
                raise WireFailure("rejected", status)
            yield response
        except (TimeoutError, httpx.TimeoutException):
            raise WireFailure("timeout") from None
        except httpx.DecodingError:
            raise invalid() from None
        except httpx.RequestError:
            raise WireFailure("unavailable") from None
        finally:
            if response is not None:
                try:
                    with anyio.move_on_after(self.policy.timeout, shield=True):
                        await response.aclose()
                except (TimeoutError, httpx.HTTPError):
                    pass

    async def chunks(self, response: httpx.Response, deadline: float) -> AsyncIterator[bytes]:
        size = 0
        iterator = response.aiter_bytes()
        while True:
            try:
                async with asyncio.timeout(self.remaining(deadline)):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            size += len(chunk)
            if size > self.policy.max_response_bytes:
                raise invalid()
            yield chunk

    async def retry(self, failure: WireFailure, number: int, deadline: float) -> bool:
        if failure.kind not in ("timeout", "unavailable", "rate_limited") or number >= self.policy.max_attempts:
            return False
        if asyncio.get_running_loop().time() + self.policy.retry_delay >= deadline:
            return False
        await asyncio.sleep(self.policy.retry_delay)
        return True

    async def json(
        self,
        endpoint: Endpoint,
        path: str,
        payload: dict[str, Any],
        decode: Callable[[Any], T],
        attempts: list[Attempt],
        deadline: float,
    ) -> T:
        for number in range(1, self.policy.max_attempts + 1):
            try:
                async with self.open(endpoint, path, payload, deadline) as response:
                    raw = bytearray()
                    async for chunk in self.chunks(response, deadline):
                        raw.extend(chunk)
                    value = decode(decode_json(bytes(raw)))
                attempts.append(Attempt(endpoint.provider, endpoint.model))
                return value
            except WireFailure as exc:
                attempts.append(exc.attempt(endpoint))
                if not await self.retry(exc, number, deadline):
                    break
        raise InferenceFailure(tuple(attempts)) from None
