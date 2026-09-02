from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "runtime.sql"
LOCAL_TEST_HOSTS = {"127.0.0.1", "::1", "localhost", "api-postgres"}
TEST_DATABASE = "assistant_rh_api_test"


def _validate_synthetic_dsn(dsn: str) -> None:
    parameters = conninfo_to_dict(dsn)
    host = parameters.get("host", "")
    database = parameters.get("dbname", "")
    if host not in LOCAL_TEST_HOSTS or database != TEST_DATABASE:
        raise RuntimeError(
            "API_SYNTHETIC_POSTGRES_DSN must target the local "
            f"{TEST_DATABASE!r} database; refusing to apply test fixtures"
        )


@pytest.fixture(scope="session")
def synthetic_database_dsn() -> str:
    dsn = os.getenv("API_SYNTHETIC_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("API_SYNTHETIC_POSTGRES_DSN is not configured")
    _validate_synthetic_dsn(dsn)
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(FIXTURE_SQL.read_text(encoding="utf-8"))
    return dsn


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
