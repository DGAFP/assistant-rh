"""Atomic finalization of a completed run, its served sources and stage events."""

import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from assistant_rh_api.core.ports import ChatRunStorePort
from assistant_rh_api.core.runtime import ChatRun, RunSource, TraceEvent
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.revisions import freeze_json


def json_data(value: object) -> object:
    """Detach domain values for JSONB without accepting arbitrary SQL columns."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: json_data(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {key: json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_data(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def as_jsonb(value: object) -> Jsonb:
    payload = json_data(value)
    json.dumps(payload, allow_nan=False)
    return Jsonb(payload)


def aware(timestamp: datetime | None) -> datetime | None:
    # Historical chat timestamps are naive UTC; new records require aware UTC.
    if timestamp is None:
        return None
    return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp


def trace_event(row: dict) -> TraceEvent:
    return TraceEvent(
        row["stage"],
        row["duration_ms"],
        row["status"],
        row["attempt_name"],
        freeze_json(row["input_ref"]),
        freeze_json(row["output_ref"]),
        freeze_json(row["metrics"]),
        row["error_type"],
        row["error_message"],
    )


class ChatRunStore(ChatRunStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def finalize(self, run: ChatRun) -> None:
        if not re.fullmatch(r"chatcmpl-[0-9a-f]{32}", run.turn_id):
            raise ValueError("new completion IDs must contain a full UUID")
        if run.timestamp is None or run.timestamp.tzinfo is None:
            raise ValueError("run timestamp must be timezone aware")
        async with self._database.transaction() as connection:
            # INSERT deliberately refuses collisions; it never overwrites a run
            # or changes the ownership/source authority of an existing answer.
            await connection.execute(
                """
                INSERT INTO public.chat_runs
                    (turn_id, trace_id, ts, user_group, api_session_hash, conversation_id, question, answer,
                     selected_ministry, model, retrieved, api_record)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    run.turn_id,
                    run.trace_id,
                    run.timestamp.astimezone(timezone.utc).replace(tzinfo=None),
                    run.group_slug,
                    run.session_hash,
                    run.conversation_id,
                    run.question,
                    run.answer,
                    run.selected_ministry,
                    run.model,
                    as_jsonb([{"id": s.doc_ref, "source": s.title, "doc_title": s.title, "url": s.url} for s in run.sources]),
                    as_jsonb(run),
                ),
            )
            for ordinal, source in enumerate(run.sources):
                await connection.execute(
                    """
                    INSERT INTO public.chat_run_sources (turn_id, ordinal, doc_ref, document_id, title, url)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                    (run.turn_id, ordinal, source.doc_ref, source.document_id, source.title, source.url),
                )
            for index, event in enumerate(run.events):
                await connection.execute(
                    """
                    INSERT INTO public.rag_trace_events
                        (turn_id, trace_id, event_index, stage, duration_ms, status, attempt_name,
                         input_ref, output_ref, metrics, error_type, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        run.turn_id,
                        run.trace_id,
                        index,
                        event.stage,
                        event.duration_ms,
                        event.status,
                        event.attempt_name,
                        as_jsonb(event.input_ref),
                        as_jsonb(event.output_ref),
                        as_jsonb(event.metrics),
                        event.error_type,
                        event.error_message,
                    ),
                )

    async def get(self, turn_id: str) -> ChatRun | None:
        async with self._database.transaction(read_only=True) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SELECT * FROM public.chat_runs WHERE turn_id = %s", (turn_id,))
                row = await cursor.fetchone()
                if row is None:
                    return None
                await cursor.execute(
                    """
                    SELECT doc_ref, title, url, document_id FROM public.chat_run_sources WHERE turn_id = %s ORDER BY ordinal
                """,
                    (turn_id,),
                )
                sources = tuple(RunSource(**r) for r in await cursor.fetchall())
                await cursor.execute(
                    """
                    SELECT stage, duration_ms, status, attempt_name, input_ref, output_ref, metrics, error_type, error_message
                    FROM public.rag_trace_events WHERE turn_id = %s ORDER BY event_index, id
                """,
                    (turn_id,),
                )
                events = tuple(trace_event(row) for row in await cursor.fetchall())
        record = row.get("api_record") or {}
        return ChatRun(
            row["turn_id"],
            row.get("trace_id") or "",
            aware(row["ts"]),
            row.get("user_group") or "",
            row.get("api_session_hash") or "",
            row.get("conversation_id") or "",
            row.get("question") or "",
            row.get("answer") or "",
            row.get("selected_ministry") or "",
            row.get("model") or "",
            sources,
            events,
            freeze_json(record.get("diagnostics")),
        )

    async def sources(self, turn_id: str, group_slug: str) -> tuple[RunSource, ...]:
        async with self._database.transaction(read_only=True) as connection:
            rows = await (
                await connection.execute(
                    """
                SELECT s.doc_ref, s.title, s.url, s.document_id FROM public.chat_run_sources s
                JOIN public.chat_runs r ON r.turn_id = s.turn_id
                WHERE r.turn_id = %s AND r.user_group = %s ORDER BY s.ordinal
            """,
                    (turn_id, group_slug),
                )
            ).fetchall()
        return tuple(RunSource(*r) for r in rows)
