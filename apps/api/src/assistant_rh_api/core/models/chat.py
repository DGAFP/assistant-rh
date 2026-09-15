"""Request-owned execution state and values shared by C6 and future C7."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, cast

from assistant_rh_api.core.models.configuration import JsonValue
from assistant_rh_api.core.models.context import ContextItem, freeze_value
from assistant_rh_api.core.models.conversations import TraceEvent
from assistant_rh_api.core.models.inference import Message, TokenUsage
from assistant_rh_api.core.ports.system import ClockPort


def evidence(value: object) -> JsonValue:
    """Detach stage evidence, never serialize a service/auth context or exception."""
    if is_dataclass(value) and not isinstance(value, type):
        return freeze_value({f.name: evidence(getattr(value, f.name)) for f in fields(value)})
    if isinstance(value, Mapping):
        return freeze_value({str(key): evidence(child) for key, child in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(evidence(child) for child in value)
    if isinstance(value, Enum):
        return evidence(value.value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return cast(JsonValue, value)
    raise TypeError("unsupported stage evidence")


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

    async def stage[T](self, name: str, operation: Callable[[], Awaitable[T]], *, attempt: str = "") -> T:
        self.cancellation.checkpoint()
        start = self.clock.monotonic()
        try:
            await self.publish(name, "started", attempt)
            result = await operation()
            self.cancellation.checkpoint()
        except (Exception, asyncio.CancelledError) as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            self.events.append(
                TraceEvent(
                    name,
                    max(0, int((self.clock.monotonic() - start) * 1000)),
                    status,
                    attempt,
                    error_type="cancelled" if status == "cancelled" else "stage_failed",
                )
            )
            try:
                await self.publish(name, "cancelled" if status == "cancelled" else "failed", attempt)
            except (Exception, asyncio.CancelledError):
                # A disconnected observer must not replace the stage's failure.
                pass
            raise
        self.events.append(TraceEvent(name, max(0, int((self.clock.monotonic() - start) * 1000)), "ok", attempt, output_ref=evidence(result)))
        await self.publish(name, "completed", attempt)
        return result


@dataclass(frozen=True, slots=True)
class PipelineResult:
    answer: str
    items: tuple[ContextItem, ...] = ()
    usage: TokenUsage = TokenUsage(0, 0, 0)
