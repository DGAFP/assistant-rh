"""One-shot reconciliation for a scheduled Scaleway Job, independent of Streamlit."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from .db_helpers import get_dsn
from .feedback_grist import FeedbackGristClient, FeedbackGristConfig, FeedbackGristError, build_feedback_records, feedback_source_environment
from .feedback_source import FEEDBACK_SELECT

# Session lock: released on disconnect, including a killed or timed-out job.
# Shared by scheduled invocations against the same source database.
SYNC_LOCK = 567561


def parse_since(value: str) -> datetime:
    """Date-only boundaries mean midnight in Paris, just like the dashboard."""
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date de début requise au format ISO (ex. 2026-08-21).") from None
    return instant if instant.tzinfo else instant.replace(tzinfo=ZoneInfo("Europe/Paris"))


def reconcile(since: datetime, *, dry_run: bool = False) -> dict:
    # Legacy feedback timestamps store UTC without a timezone. Bind the same
    # PostgreSQL type so the session TimeZone cannot shift the comparison.
    since = since if since.tzinfo else since.replace(tzinfo=ZoneInfo("Europe/Paris"))
    cutoff = since.astimezone(timezone.utc).replace(tzinfo=None)
    config = FeedbackGristConfig.from_env()
    environment = feedback_source_environment()
    with psycopg.connect(
        get_dsn(),
        connect_timeout=10,
        autocommit=True,
        options="-c default_transaction_read_only=on -c statement_timeout=30000",
        row_factory=dict_row,
    ) as connection:
        if not connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (SYNC_LOCK,)).fetchone()["acquired"]:
            return {"status": "already_running", "environment": environment}
        rows = connection.execute(FEEDBACK_SELECT + " WHERE f.ts >= %s ORDER BY f.ts, f.id", (cutoff,)).fetchall()
        data = pd.DataFrame(rows)
        # Validate even in dry-run mode, without contacting Grist.
        records = build_feedback_records(data, environment)
        report = {
            "environment": environment,
            "since": since.isoformat(),
            "total": len(records),
            "groups": data["user_group"].fillna("unknown").value_counts().to_dict() if rows else {},
        }
        if dry_run:
            return {**report, "status": "dry_run"}
        result = FeedbackGristClient(config).sync(data, environment)
        return {**report, "status": "error" if result.error else "synced", "synced": result.synced, "error": result.error}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchroniser tous les feedbacks du périmètre vers Grist, sans toucher aux annotations humaines.")
    parser.add_argument("--since", type=parse_since, default=os.getenv("GRIST_FEEDBACK_SINCE"), help="Date ISO inclusive ; ou GRIST_FEEDBACK_SINCE.")
    parser.add_argument("--dry-run", action="store_true", help="Lire et valider le périmètre sans contacter Grist.")
    args = parser.parse_args(argv)
    if args.since is None:
        parser.error("--since ou GRIST_FEEDBACK_SINCE est obligatoire : aucun élargissement implicite à tout l’historique.")
    try:
        report = reconcile(args.since, dry_run=args.dry_run)
    except FeedbackGristError as exc:
        report = {"status": "error", "error": str(exc)}
    except Exception as exc:
        # DB/provider exceptions can contain DSNs, queries or feedback content.
        report = {"status": "error", "error": f"Échec de synchronisation ({type(exc).__name__})."}
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
