"""#604: staging's varchar(8) conversation_id must accept API correlation IDs."""

from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from apps.api.tests.db.conftest import ROOT

MIGRATION = ROOT / "supabase/migrations/20261007100000_chat_runs_conversation_id_text.sql"
INSERT = "INSERT INTO public.chat_runs (turn_id, conversation_id, question, answer) VALUES (%s, %s, 'Q', 'A')"
UUID_ID = "6f1c2a9e-4b7d-4e8a-9c3f-2d5b8a7e1f04"


def test_migration_widens_the_staging_column_without_losing_rows(repository_dsn):
    assert isinstance(MIGRATION, Path) and MIGRATION.exists()
    with psycopg.connect(repository_dsn) as connection:
        # Reproduce staging inside a transaction that is rolled back.
        connection.execute("ALTER TABLE public.chat_runs ALTER COLUMN conversation_id TYPE VARCHAR(8)")
        connection.execute(INSERT, ("legacy-604", "abcd1234"))
        with connection.transaction():
            with pytest.raises(errors.StringDataRightTruncation):
                with connection.transaction():
                    connection.execute(INSERT, ("api-604-before", UUID_ID))
        connection.execute(MIGRATION.read_text())
        connection.execute(INSERT, ("api-604", UUID_ID))
        rows = connection.execute(
            "SELECT turn_id, conversation_id FROM public.chat_runs WHERE turn_id IN ('legacy-604', 'api-604') ORDER BY turn_id"
        ).fetchall()
        kind = connection.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_name = 'chat_runs' AND column_name = 'conversation_id'"
        ).fetchone()
        connection.rollback()
    assert rows == [("api-604", UUID_ID), ("legacy-604", "abcd1234")]
    assert kind == ("text",)
