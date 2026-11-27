"""The operator probe exercises real role guards without retaining its rows."""

import psycopg
import pytest
from psycopg.rows import dict_row

from apps.api.tests.db.test_runtime_roles import logins  # noqa: F401
from scripts.conformance.m2_runtime_roles import probe


@pytest.mark.usefixtures("logins")
def test_operator_probe_checks_every_role_and_rolls_back_fixtures(repository_dsn):
    with psycopg.connect(repository_dsn, row_factory=dict_row) as connection:
        tables = ("chat_runs", "chat_feedbacks", "chat_feedback_audit")
        before = {table: connection.execute(f"SELECT count(*) AS count FROM public.{table}").fetchone()["count"] for table in tables}
        report = probe(connection)
        assert report["complete"] and report["passed"] and report["gate"] == "pass", report
        assert report["cleanup"] == {"fixture_rows_remaining": 0, "transaction_rolled_back": True}
        after = {table: connection.execute(f"SELECT count(*) AS count FROM public.{table}").fetchone()["count"] for table in tables}
        assert before == after


@pytest.mark.usefixtures("logins")
def test_api_login_cannot_turn_partial_role_coverage_into_a_go(repository_dsn):
    with psycopg.connect(repository_dsn, row_factory=dict_row) as connection:
        connection.execute("SET ROLE test_rt_api")
        report = probe(connection)
        assert report["passed"] and not report["complete"] and report["gate"] == "incomplete", report
        assert report["role_exercises"]["arh_api"] == "exercised"
        assert report["cleanup"]["fixture_rows_remaining"] == 0
