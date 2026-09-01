from __future__ import annotations

import re
from pathlib import Path

FIXTURE_SQL = Path(__file__).parents[1] / "fixtures" / "runtime.sql"


def test_runtime_fixture_contains_only_allowlisted_synthetic_tables() -> None:
    sql = FIXTURE_SQL.read_text(encoding="utf-8")
    created_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", sql, flags=re.IGNORECASE))

    assert created_tables == {"rag_config"}
    assert "synthetic-fixture" in sql


def test_runtime_fixture_excludes_personal_data_domains() -> None:
    sql = FIXTURE_SQL.read_text(encoding="utf-8").lower()
    statements = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)

    for forbidden_name in ("chat_runs", "feedback", "user_groups", "email", "password_hash", "original_turn_id"):
        assert forbidden_name not in statements
