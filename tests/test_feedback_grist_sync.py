"""Scheduled reconciliation: scope, repeated updates, isolation and failures."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock

import psycopg
import pytest
from assistant_rh_rag_pipeline import feedback_grist_sync as job
from test_feedback_grist import GristServer


@pytest.fixture
def source(monkeypatch):
    for key, value in {
        "GRIST_API_BASE_URL": "https://grist.example.test",
        "GRIST_FEEDBACK_DOC_ID": "feedback-doc",
        "GRIST_FEEDBACK_TABLE_ID": "Feedbacks",
        "GRIST_API_KEY": "secret-key",
        "APP_SCALEWAY_ENV": "production",
        "APP_DB_TARGET": "scaleway",
        "SCW_POSTGRES_DSN": "postgresql://secret-dsn",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GRIST_FEEDBACK_SINCE", raising=False)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE chat_feedbacks (
        id INTEGER, ts TEXT, turn_id TEXT, turn_idx INTEGER, helpful BOOLEAN, reasons TEXT,
        reasons_positive TEXT, reasons_negative TEXT, comment TEXT, stars INTEGER, session_id TEXT,
        question TEXT, answer TEXT, error_category TEXT, ai_reason TEXT, ai_analyzed_at TEXT,
        beta_scope TEXT, theme TEXT)""")
    db.execute("""CREATE TABLE chat_runs (
        turn_id TEXT, question TEXT, answer TEXT, v3_detected_theme TEXT, user_group TEXT,
        selected_ministry TEXT, dist_after_rerank TEXT, rag_version TEXT, chunk_selection_mode TEXT,
        total_time_ms INTEGER)""")
    db.execute("INSERT INTO chat_runs (turn_id, question, answer, user_group) VALUES ('t1', 'Question', 'Réponse', 'new-group')")
    db.executemany(
        "INSERT INTO chat_feedbacks (id, ts, turn_id) VALUES (?, ?, ?)",
        [(1, "2026-08-20T23:59:59+02:00", None), (2, "2026-08-21T00:00:00+02:00", "t1"), (3, "2026-09-21T12:00:00+02:00", None)],
    )
    server = GristServer()
    monkeypatch.setattr("requests.request", server.request)
    connection = Mock()

    def execute(query, params):
        if "pg_try_advisory_lock" in query:
            return Mock(fetchone=lambda: {"acquired": True})
        cursor = db.execute(query.replace("%s", "?"), (params[0].isoformat(),))
        return Mock(fetchall=lambda: [dict(row) for row in cursor.fetchall()])

    connection.execute.side_effect = execute

    @contextmanager
    def connect(*args, **kwargs):
        assert kwargs["autocommit"] is True
        assert "default_transaction_read_only=on" in kwargs["options"]
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(job.psycopg, "connect", connect)
    yield db, server, connection
    db.close()


def test_new_groups_orphans_and_updates_are_reconciled_without_touching_annotations(source):
    db, server, connection = source
    since = job.parse_since("2026-08-21")
    first = job.reconcile(since)
    assert first["total"] == first["synced"] == 2
    assert first["groups"] == {"new-group": 1, "unknown": 1}
    assert set(server.rows) == {("production", "2"), ("production", "3")}
    assert server.rows[("production", "2")]["question"] == "Question"
    assert server.rows[("production", "2")]["answer"] == "Réponse"
    server.rows[("production", "2")].update(traite=True, hors_champs=False, missing_document=True)
    db.execute("UPDATE chat_feedbacks SET ai_reason = 'Analyse corrigée' WHERE id = 2")
    job.reconcile(since)
    assert len(server.rows) == 2
    assert server.rows[("production", "2")]["ai_reason"] == "Analyse corrigée"
    assert server.rows[("production", "2")]["traite"] is True
    assert server.rows[("production", "2")]["hors_champs"] is False
    assert server.rows[("production", "2")]["missing_document"] is True
    assert connection.close.call_count == 2


def test_dry_run_reads_scope_but_never_contacts_grist(source, capsys):
    _, server, _ = source
    assert job.main(["--since", "2026-08-21", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 2
    assert report["status"] == "dry_run"
    assert not server.calls


def test_concurrent_job_skips_and_disconnects(source):
    _, server, connection = source
    connection.execute.side_effect = lambda *args: Mock(fetchone=lambda: {"acquired": False})
    assert job.reconcile(job.parse_since("2026-08-21"))["status"] == "already_running"
    assert not server.calls
    assert connection.execute.call_count == 1
    connection.close.assert_called_once()


def test_timeout_after_write_fails_then_retries_without_duplicates(source, capsys):
    _, server, _ = source
    server.fail_after = 1
    assert job.main(["--since", "2026-08-21"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert job.main(["--since", "2026-08-21"]) == 0
    assert len(server.rows) == 2


def test_database_failure_is_nonzero_without_leaking_credentials(source, capsys):
    _, _, connection = source
    connection.execute.side_effect = psycopg.OperationalError("postgresql://secret-dsn private feedback")
    assert job.main(["--since", "2026-08-21"]) == 1
    output = capsys.readouterr().out
    assert "OperationalError" in output
    assert "secret-dsn" not in output
    assert "private feedback" not in output
    connection.close.assert_called_once()


def test_scope_is_required_before_database_access(source):
    _, _, connection = source
    with pytest.raises(SystemExit) as error:
        job.main([])
    assert error.value.code == 2
    connection.execute.assert_not_called()


def test_scope_can_be_configured_in_scaleway(source, monkeypatch, capsys):
    monkeypatch.setenv("GRIST_FEEDBACK_SINCE", "2026-08-21")
    assert job.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["since"] == "2026-08-21T00:00:00+02:00"


def test_empty_scope_does_not_create_grist_table(source, capsys):
    _, server, _ = source
    assert job.main(["--since", "2027-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0
    assert not server.calls


def test_explicit_timezone_is_preserved():
    assert job.parse_since("2026-08-21T00:00:00Z") == datetime(2026, 8, 21, tzinfo=timezone.utc)
