"""Bounded SSE workers; each response owns and joins all of its tasks."""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass

import anyio
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.chat import ChatService
from assistant_rh_api.core.models.catalog import Model
from assistant_rh_api.core.models.chat import Cancellation, ChatInput, PipelineEvent, PipelineResult
from assistant_rh_api.core.models.conversations import ChatRun
from assistant_rh_api.handlers.errors import ChatUnavailable, error_response

logger = logging.getLogger(__name__)


def extension(run: ChatRun) -> dict:
    return {
        "turn_id": run.turn_id,
        "ministry": run.selected_ministry,
        "sources": [
            {"title": s.title, "url": s.url or None, "publisher": s.publisher, "doc_ref": s.doc_ref, "access": s.access} for s in run.sources
        ],
    }


def data(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


async def _wait_for_cleanup(task: asyncio.Task[None]) -> None:
    """Wait for owned cleanup despite caller cancellation; propagate cleanup errors."""
    # AnyIO shielding handles cancel scopes; asyncio.shield handles Task.cancel().
    # Repeated cancellation must not let the caller abandon its cleanup task.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
    task.result()


@dataclass(frozen=True)
class StreamSettings:
    workers: int = 8
    queue_size: int = 32
    delta_chars: int = 4096
    ping_seconds: float = 10
    send_timeout: float = 30

    def __post_init__(self) -> None:
        if min(self.workers, self.queue_size, self.delta_chars, self.ping_seconds, self.send_timeout) <= 0:
            raise ValueError("stream limits must be positive")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "StreamSettings":
        return cls(workers=int(env.get("API_STREAM_WORKERS", "8")), queue_size=int(env.get("API_STREAM_QUEUE_SIZE", "32")))


class StreamWorkers:
    """Loop-local admission, with no unbounded queue of waiting requests."""

    def __init__(self, settings: StreamSettings = StreamSettings()) -> None:
        self.settings = settings
        self.active: set[ChatStreamResponse] = set()
        self.closed = False

    def response(self, service: ChatService, request: ChatInput, auth: AuthContext, model: Model, *, include_usage: bool) -> "ChatStreamResponse":
        if self.closed or len(self.active) >= self.settings.workers:
            raise ChatUnavailable()
        response = ChatStreamResponse(self, service, request, auth, model, include_usage=include_usage)
        self.active.add(response)
        return response

    async def aclose(self) -> None:
        self.closed = True
        await asyncio.gather(*(response.shutdown() for response in tuple(self.active)))


class ChatStreamResponse(Response):
    """Own the pipeline worker and HTTP sender for one request.

    The worker runs the pipeline and persists its outcome; the sender drains
    the queue and emits pings. The ASGI call watches the sender and disconnects,
    then joins both transport tasks and the worker in its final cleanup.
    stop() shares one worker cleanup task with shutdown(); shutdown() also waits
    for finished, which is set only after the ASGI cleanup releases admission.
    """

    media_type = "text/event-stream"

    def __init__(self, owner: StreamWorkers, service: ChatService, request: ChatInput, auth: AuthContext, model: Model, *, include_usage: bool):
        super().__init__(headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        # Response's empty body must not set Content-Length: 0 on an SSE stream.
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self.owner = owner
        self.settings = owner.settings
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.settings.queue_size)
        self.cancellation = Cancellation()
        self.context = service.new_context(sink=self, cancellation=self.cancellation)
        self.service, self.request, self.auth = service, request, auth
        self.include_usage = include_usage
        self.base = {
            "id": "chatcmpl-" + self.context.turn_id,
            "object": "chat.completion.chunk",
            "created": int(self.context.created.timestamp()),
            "model": model.id,
        }
        self.worker: asyncio.Task[tuple[ChatRun, PipelineResult]] | None = None
        self.stopping: asyncio.Task[None] | None = None
        self.finished = asyncio.Event()
        self.serving: asyncio.Task[None] | None = None
        self.shutting_down = False

    async def publish(self, event: PipelineEvent) -> None:
        self.cancellation.checkpoint()
        # Stage diagnostics already belong to RunContext; only text is sent.
        if event.phase != "delta":
            return
        for start in range(0, len(event.text), self.settings.delta_chars):
            await self.queue.put(event.text[start : start + self.settings.delta_chars])
            self.cancellation.checkpoint()

    def chunk(self, delta: dict, *, finish: str | None = None, **extra: object) -> bytes:
        return data({**self.base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})

    async def _send(self, send: Send, body: bytes) -> None:
        async with asyncio.timeout(self.settings.send_timeout):
            await send({"type": "http.response.body", "body": body, "more_body": True})

    async def _serve(self, send: Send) -> None:
        assert self.worker is not None
        async with asyncio.timeout(self.settings.send_timeout):
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        await self._send(send, self.chunk({"role": "assistant", "content": ""}))
        await self._forward_events(send)
        try:
            run, result = self.worker.result()
        except (Exception, asyncio.CancelledError):
            await self._send_error(send)
        else:
            await self._send_success(send, run, result)
        async with asyncio.timeout(self.settings.send_timeout):
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _forward_events(self, send: Send) -> None:
        """Drain events in order and keep pinging until the worker has finished."""
        assert self.worker is not None
        loop = asyncio.get_running_loop()
        next_ping = loop.time() + self.settings.ping_seconds
        while not self.worker.done() or not self.queue.empty():
            if loop.time() >= next_ping:
                await self._send(send, b": ping\n\n")
                next_ping = loop.time() + self.settings.ping_seconds
            if not self.queue.empty():
                text = self.queue.get_nowait()
            else:
                reading = asyncio.create_task(self.queue.get())
                try:
                    await asyncio.wait({reading, self.worker}, timeout=max(0, next_ping - loop.time()), return_when=asyncio.FIRST_COMPLETED)
                    if not reading.done():
                        continue
                    text = reading.result()
                finally:
                    reading.cancel()
                    await asyncio.gather(reading, return_exceptions=True)
            await self._send(send, self.chunk({"content": text}))

    async def _send_error(self, send: Send) -> None:
        await self._send(
            send,
            data({"error": {"message": "Service momentanément indisponible", "type": "server_error", "code": "stream_error"}}),
        )

    async def _send_success(self, send: Send, run: ChatRun, result: PipelineResult) -> None:
        # complete() returns only after the atomic run/source/trace commit.
        suffix = run.answer[len(result.answer) :]
        for start in range(0, len(suffix), self.settings.delta_chars):
            await self._send(send, self.chunk({"content": suffix[start : start + self.settings.delta_chars]}))
        await self._send(send, self.chunk({}, finish="stop", x_assistant_rh=extension(run)))
        if self.include_usage:
            await self._send(send, data({**self.base, "choices": [], "usage": asdict(result.usage)}))
        await self._send(send, b"data: [DONE]\n\n")

    async def _disconnect(self, receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    async def stop(self, reason: str) -> None:
        if self.stopping is None:

            async def stop_worker() -> None:
                self.context.diagnostics["cancellation"] = reason
                self.cancellation.cancel()
                if self.worker is not None:
                    if not self.worker.done():
                        self.worker.cancel()
                    # Core finalization and gateway close have bounded deadlines.
                    await asyncio.gather(self.worker, return_exceptions=True)

            self.stopping = asyncio.create_task(stop_worker(), name="chat-stop-" + self.context.turn_id)
        await _wait_for_cleanup(self.stopping)

    async def shutdown(self) -> None:
        self.shutting_down = True
        if self.serving is None:
            # Admitted but never served: there is nothing to join, and a late
            # ASGI call is refused instead of starting a worker after shutdown.
            self.owner.active.discard(self)
            self.finished.set()
            return
        await self.stop("shutdown")
        self.serving.cancel()
        await self.finished.wait()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.shutting_down:
            await error_response(503, "service_unavailable", "Service unavailable")(scope, receive, send)
            return
        self.worker = asyncio.create_task(
            self.service.complete(self.request, self.auth, context=self.context, stream=True),
            name="chat-stream-" + self.context.turn_id,
        )
        serving = self.serving = asyncio.create_task(self._serve(send))
        disconnect = asyncio.create_task(self._disconnect(receive))
        reason = "disconnect"
        try:
            await asyncio.wait({serving, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            if serving.done() and not self.shutting_down:
                serving.result()
        except (OSError, TimeoutError):
            reason = "send_failed"
            logger.warning("Chat stream send failed (turn_id=%s)", self.context.turn_id)
        except asyncio.CancelledError:
            # Only the server cancels the ASGI task (graceful-shutdown deadline).
            reason = "shutdown"
            raise
        finally:
            # Run the whole cleanup in an independent task, not the cancelled ASGI scope.
            async def cleanup() -> None:
                serving.cancel()
                disconnect.cancel()
                await asyncio.gather(serving, disconnect, return_exceptions=True)
                # A finished worker has already persisted its outcome; nothing to stop.
                if self.worker is None or not self.worker.done():
                    await self.stop(reason)
                self.owner.active.discard(self)
                self.finished.set()

            closing = asyncio.create_task(cleanup())
            await _wait_for_cleanup(closing)
