"""Correlate backend calls without logging identity, bearer or delegation claims."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from assistant_rh_api.core.observability import observing
from assistant_rh_api.telemetry import Telemetry, route_name

logger = logging.getLogger(__name__)
REQUEST_ID = re.compile(rb"[0-9a-f]{32}")


class RequestCorrelation:
    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        candidates = [value for name, value in scope.get("headers", ()) if name == b"x-client-request-id"]
        request_id = candidates[0].decode("ascii") if len(candidates) == 1 and REQUEST_ID.fullmatch(candidates[0]) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        route = route_name(scope.get("path", ""))
        method = scope.get("method", "OTHER")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            method = "OTHER"
        status = 500
        finished_at = None
        outcome = "incomplete"
        self.telemetry.active.labels(route).inc()

        async def correlated_send(message: Message) -> None:
            nonlocal status, finished_at
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(name, value) for name, value in message.get("headers", ()) if name.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
                logger.info("api_request request_id=%s status=%s", request_id, message["status"])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished_at = time.monotonic()

        try:
            with observing(lambda event: self.telemetry.observation(request_id, event)):
                await self.app(scope, receive, correlated_send)
            outcome = "completed" if finished_at is not None and status < 500 else "failed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            # Starlette re-raises after sending a safe 500. Keep Uvicorn's error
            # handling (including closing incomplete streams), without the original
            # exception message, traceback or chained exceptions in its logs.
            raise RuntimeError(f"API request failed (request_id={request_id})") from None
        finally:
            elapsed = max(0, (finished_at or time.monotonic()) - started)
            stream = scope["state"].get("chat_stream_observation")
            if stream is not None:
                stream_outcome = stream["outcome"]
                self.telemetry.streams.labels(stream_outcome).inc()
                if stream.get("first_text_at") is not None:
                    self.telemetry.first_text.observe(max(0, stream["first_text_at"] - started))
                outcome = "completed" if stream_outcome == "completed" else "failed" if stream_outcome == "failed" else "cancelled"
            elif scope["state"].get("chat_stream"):
                self.telemetry.streams.labels("rejected").inc()
            self.telemetry.requests.labels(route, method, str(status), outcome).inc()
            self.telemetry.duration.labels(route, outcome).observe(elapsed)
            self.telemetry.active.labels(route).dec()
            logger.info(
                "api_request_finished request_id=%s route=%s method=%s status=%s outcome=%s duration_ms=%d",
                request_id,
                route,
                method,
                status,
                outcome,
                int(elapsed * 1000),
            )
