"""Request-owned execution state and values shared by C6 and future C7."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.configuration import JsonValue
from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.models.conversations import TraceEvent
from assistant_rh_api.core.models.inference import Message, TokenUsage
from assistant_rh_api.core.ports.system import ClockPort
from assistant_rh_api.core.trace_values import attempt_trace, trace_payload


@dataclass(frozen=True, slots=True)
class ChatInput:
    model: str
    question: str
    history: tuple[Message, ...] = ()
    conversation_id: str = ""


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    turn_id: str
    stage: str
    phase: Literal["started", "completed", "failed", "cancelled", "delta"]
    attempt_name: str = ""
    text: str = ""


class EventSinkPort(Protocol):
    async def publish(self, event: PipelineEvent) -> None:
        """Observe progress with backpressure; a future C7 sink owns its queue."""
        ...


class CancellationPort(Protocol):
    def checkpoint(self) -> None:
        """Raise asyncio.CancelledError when cooperative cancellation is requested."""
        ...


class Cancellation:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def checkpoint(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError()


@dataclass(slots=True)
class RunContext:
    turn_id: str
    trace_id: str
    created: datetime
    today: str
    clock: ClockPort
    cancellation: CancellationPort = field(default_factory=Cancellation)
    sink: EventSinkPort | None = None
    events: list[TraceEvent] = field(default_factory=list)
    diagnostics: dict[str, JsonValue] = field(default_factory=dict)
    partial_answer: str = ""

    async def publish(self, stage: str, phase: Literal["started", "completed", "failed", "cancelled", "delta"], attempt: str = "") -> None:
        if self.sink is not None:
            await self.sink.publish(PipelineEvent(self.turn_id, stage, phase, attempt))

    async def stage[T](self, name: str, operation: Callable[[], Awaitable[T]], *, project: Callable[[T], JsonValue], attempt: str = "") -> T:
        self.cancellation.checkpoint()
        start = self.clock.monotonic()
        try:
            await self.publish(name, "started", attempt)
            result = await operation()
            self.cancellation.checkpoint()
            output = trace_payload(project(result))
        except (Exception, asyncio.CancelledError) as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            self.events.append(
                TraceEvent(
                    stage=name,
                    duration_ms=max(0, int((self.clock.monotonic() - start) * 1000)),
                    status=status,
                    attempt_name=attempt,
                    metrics=trace_payload({"inference_attempts": attempt_trace(exc.attempts)}) if isinstance(exc, InferenceFailure) else None,
                    error_type="cancelled" if status == "cancelled" else "stage_failed",
                )
            )
            try:
                await self.publish(name, "cancelled" if status == "cancelled" else "failed", attempt)
            except (Exception, asyncio.CancelledError):
                # A disconnected observer must not replace the stage's failure.
                pass
            raise
        self.events.append(
            TraceEvent(
                stage=name,
                duration_ms=max(0, int((self.clock.monotonic() - start) * 1000)),
                status="ok",
                attempt_name=attempt,
                output_ref=output,
            )
        )
        await self.publish(name, "completed", attempt)
        return result


@dataclass(frozen=True, slots=True)
class PipelineResult:
    answer: str
    items: tuple[ContextItem, ...] = ()
    usage: TokenUsage = TokenUsage(0, 0, 0)
