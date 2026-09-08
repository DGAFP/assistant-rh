"""Current feedback, lossless audit, and optimistic analysis writes."""

from datetime import datetime, timezone

from psycopg.rows import dict_row

from assistant_rh_api.core.errors import DatabaseFailure
from assistant_rh_api.core.ports import FeedbackStorePort
from assistant_rh_api.core.runtime import Feedback, FeedbackAnalysisData, FeedbackInput
from assistant_rh_api.db.content_store import immutable_object
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.revisions import content_revision, freeze_json
from assistant_rh_api.db.run_store import as_jsonb, aware, json_data


def _decode_reasons(value: str | None) -> tuple[str, ...]:
    """Decode the historical Streamlit TEXT representation at the DB boundary."""
    return tuple(reason.strip() for reason in (value or "").split(";") if reason.strip())


def _encode_reasons(reasons: tuple[str, ...]) -> str:
    # Refuse values that cannot round-trip through the legacy delimiter format.
    if not isinstance(reasons, tuple) or any(
        not isinstance(reason, str) or not reason or reason != reason.strip() or ";" in reason for reason in reasons
    ):
        raise ValueError("reasons must be a tuple of nonempty trimmed strings without semicolons")
    return "; ".join(reasons)


def feedback(row: dict) -> Feedback:
    value = FeedbackInput(
        row["turn_id"],
        row["stars"] + 1 if row.get("stars") is not None else None,
        row.get("comment") or "",
        _decode_reasons(row.get("reasons_positive")),
        _decode_reasons(row.get("reasons_negative")),
        row.get("helpful"),
    )
    # Explicit edit generation also distinguishes A -> B -> A with a fixed clock.
    revision = content_revision(freeze_json([row["id"], row.get("api_revision", 0), json_data(value)]))
    annotations = immutable_object({key: row.get(key) for key in ("beta_scope", "theme")})
    return Feedback(row["id"], value, aware(row["ts"]), revision, row.get("error_category"), row.get("ai_reason"), annotations)


class FeedbackStore(FeedbackStorePort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, turn_id: str) -> Feedback | None:
        async with self._database.transaction(read_only=True) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM public.chat_feedbacks WHERE turn_id = %s ORDER BY ts DESC NULLS LAST, id DESC LIMIT 1", (turn_id,)
                )
                row = await cursor.fetchone()
        return feedback(row) if row else None

    async def save(self, value: FeedbackInput, group_slug: str, session_hash: str, now: datetime) -> Feedback | None:
        if not session_hash or now.tzinfo is None:
            raise ValueError("session hash and aware timestamp required")
        reasons_positive = _encode_reasons(value.reasons_positive)
        reasons_negative = _encode_reasons(value.reasons_negative)
        async with self._database.transaction() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                # The parent exists before any feedback: this also serializes
                # concurrent first submissions (locking an absent child cannot).
                # NO KEY UPDATE still serializes API writers, but lets a legacy
                # INSERT holding the advisory lock finish its FK KEY SHARE check.
                await cursor.execute(
                    """
                    SELECT question, answer FROM public.chat_runs
                    WHERE turn_id = %s AND user_group = %s FOR NO KEY UPDATE
                """,
                    (value.turn_id, group_slug),
                )
                run = await cursor.fetchone()
                if run is None:
                    return None
                # Same ordering as the compatibility trigger, before child lock.
                await cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 454))", (value.turn_id,))
                await cursor.execute("SELECT * FROM public.chat_feedbacks WHERE turn_id = %s FOR UPDATE", (value.turn_id,))
                old = await cursor.fetchone()
                if old and feedback(old).value == value:
                    return feedback(old)
                if old:
                    await cursor.execute(
                        """
                        INSERT INTO public.chat_feedback_audit(turn_id, feedback_id, reason, record, group_slug, audit_session_hash)
                        VALUES (%s, %s, 'API replacement', %s, %s, %s)
                    """,
                        (value.turn_id, old["id"], as_jsonb(old), group_slug, session_hash),
                    )
                timestamp = now.astimezone(timezone.utc).replace(tzinfo=None)
                common = (
                    timestamp,
                    value.stars - 1 if value.stars is not None else None,
                    value.comment,
                    reasons_positive,
                    reasons_negative,
                    value.helpful,
                    group_slug,
                    session_hash,
                )
                if old:
                    await cursor.execute(
                        """
                        UPDATE public.chat_feedbacks SET ts = %s, stars = %s, comment = %s,
                            reasons_positive = %s, reasons_negative = %s, helpful = %s, api_group_slug = %s, api_session_hash = %s,
                            error_category = NULL, ai_reason = NULL, ai_analyzed_at = NULL, api_revision = api_revision + 1
                        WHERE id = %s RETURNING *
                    """,
                        (*common, old["id"]),
                    )
                else:
                    await cursor.execute(
                        """
                        INSERT INTO public.chat_feedbacks
                            (ts, stars, comment, reasons_positive, reasons_negative, helpful, api_group_slug, api_session_hash,
                             turn_id, question, answer)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                        (*common, value.turn_id, run["question"], run["answer"]),
                    )
                result = await cursor.fetchone()
                if result is None:
                    raise DatabaseFailure()
        return feedback(result)

    async def for_analysis(self, max_stars: int, limit: int) -> tuple[FeedbackAnalysisData, ...]:
        if not 1 <= max_stars <= 5 or not 1 <= limit <= 1000:
            raise ValueError("invalid analysis bounds")
        async with self._database.transaction(read_only=True) as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT f.*, to_jsonb(r) AS run_context FROM public.chat_feedbacks f
                    LEFT JOIN public.chat_runs r ON r.turn_id = f.turn_id
                    WHERE f.stars <= %s AND f.error_category IS NULL
                    ORDER BY f.ts DESC NULLS LAST, f.id DESC LIMIT %s
                """,
                    (max_stars - 1, limit),
                )
                rows = await cursor.fetchall()
        return tuple(
            FeedbackAnalysisData(
                feedback(r), r.get("question") or "", r.get("answer") or "", immutable_object(r["run_context"]) if r["run_context"] else None
            )
            for r in rows
        )

    async def save_analysis(self, feedback_id: int, revision: str, category: str, reason: str, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("analysis timestamp must be timezone aware")
        async with self._database.transaction() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SELECT * FROM public.chat_feedbacks WHERE id = %s FOR UPDATE", (feedback_id,))
                row = await cursor.fetchone()
                if not row or feedback(row).revision != revision or row.get("error_category") is not None:
                    return False
                await cursor.execute(
                    "UPDATE public.chat_feedbacks SET error_category = %s, ai_reason = %s, ai_analyzed_at = %s WHERE id = %s",
                    (category, reason, now, feedback_id),
                )
        return True
