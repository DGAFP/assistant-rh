from pathlib import Path

import psycopg
import pytest
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database

ROOT = Path(__file__).resolve().parents[4]
BASELINE = ROOT / "apps/api/tests/fixtures/repositories.sql"
TRACE_MIGRATION = ROOT / "supabase/migrations/20260625110000_rag_trace_events.sql"
MIGRATION = ROOT / "supabase/migrations/20260908094542_api_runtime_repositories.sql"
AUTH_MIGRATION = ROOT / "supabase/migrations/20260908180029_api_public_auth.sql"


@pytest.fixture(scope="session")
def repository_dsn(synthetic_database_dsn):
    with psycopg.connect(synthetic_database_dsn) as connection:
        connection.execute(BASELINE.read_text())
        connection.execute(TRACE_MIGRATION.read_text())
        connection.execute(MIGRATION.read_text())
        connection.execute(AUTH_MIGRATION.read_text())
    return synthetic_database_dsn


@pytest.fixture
async def repository_db(repository_dsn):
    database = Database(DatabaseSettings(dsn=repository_dsn, max_size=4, timeout_seconds=1))
    await database.open()
    try:
        async with database.transaction() as connection:
            await connection.execute("""
                TRUNCATE public.chat_run_sources, public.chat_feedback_audit, public.chat_feedbacks, public.chat_runs,
                    public.rag_trace_events, public.api_auth_limits, public.api_sessions, public.user_groups, public.system_prompts, public.acronyms,
                    public.rag_chunks_service_public, public.rag_chunks_matte, public.rag_chunks_dgafp,
                    public.rag_chunks_mso, public.rag_chunks_mi, public.rag_chunks_masa, public.rag_chunks_rgrh,
                    public.rag_sections, public.rag_documents RESTART IDENTITY;
                INSERT INTO public.user_groups(slug, label, password_hash) VALUES ('synthetic', 'Synthetic', 'fixture-hash');
            """)
        yield database
    finally:
        await database.close()
