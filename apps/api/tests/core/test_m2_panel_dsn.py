"""Staging target selection stays explicit even for read-only qualification."""

import pytest
from assistant_rh_api.core.errors import DatabaseConfigurationError
from psycopg.conninfo import conninfo_to_dict

from scripts.conformance.m2_panel import readonly_staging_dsn


def test_staging_dsn_remains_read_only(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SCW_POSTGRES_DSN_STAGING='host=staging.example dbname=arh user=test sslmode=require'\n")

    dsn = conninfo_to_dict(readonly_staging_dsn(path))

    assert dsn["host"] == "staging.example" and dsn["sslmode"] == "require"
    assert "-c default_transaction_read_only=on" in dsn["options"]


@pytest.mark.parametrize("variable", ("PGSERVICE", "PGHOSTADDR"))
def test_ambient_libpq_target_is_rejected(monkeypatch, tmp_path, variable):
    monkeypatch.setenv(variable, "203.0.113.6")
    path = tmp_path / ".env"
    path.write_text("SCW_POSTGRES_DSN_STAGING='host=staging.example dbname=arh user=test'\n")

    with pytest.raises(DatabaseConfigurationError):
        readonly_staging_dsn(path)


def test_service_only_dsn_is_rejected(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SCW_POSTGRES_DSN_STAGING='service=staging'\n")

    with pytest.raises(DatabaseConfigurationError):
        readonly_staging_dsn(path)


def test_production_match_uses_default_port_and_hostaddr(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "SCW_POSTGRES_DSN_STAGING='host=stage.example hostaddr=203.0.113.6 dbname=arh user=test'\n"
        "SCW_POSTGRES_DSN_PROD='host=203.0.113.6 port=5432 dbname=arh user=app'\n"
    )

    with pytest.raises(ValueError, match="matches production"):
        readonly_staging_dsn(path)
