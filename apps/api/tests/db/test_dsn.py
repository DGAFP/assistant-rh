import pytest
from assistant_rh_api.core.errors import DatabaseConfigurationError
from assistant_rh_api.db.dsn import DatabaseSettings, resolve_dsn
from assistant_rh_api.db.pool import Database, _SafeConnection

DSN = "postgresql://synthetic:private-marker@localhost/assistant_rh_api_test"


def test_resolution_is_explicit_and_does_not_use_ambient_environment(monkeypatch) -> None:
    monkeypatch.setenv("SCW_POSTGRES_DSN", DSN)
    monkeypatch.setenv("PGHOST", "unexpected")
    with pytest.raises(DatabaseConfigurationError):
        resolve_dsn()
    assert resolve_dsn(environ={"SCW_POSTGRES_DSN": DSN}) == DSN
    assert resolve_dsn(dsn=DSN, environ={"SCW_POSTGRES_DSN": "invalid"}) == DSN
    with pytest.raises(DatabaseConfigurationError):
        resolve_dsn(dsn="", environ={"SCW_POSTGRES_DSN": DSN})
    with pytest.raises(DatabaseConfigurationError):
        resolve_dsn(environ={"SCALINGO_POSTGRESQL_URL": DSN})


@pytest.mark.parametrize("dsn", ["", "private-marker", "postgresql:///db", "host=localhost dbname=test", "service=private-marker"])
def test_invalid_dsn_errors_do_not_leak_input(dsn: str) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseSettings(dsn=dsn)
    assert str(caught.value) == "database_configuration_error"
    assert "private-marker" not in repr(caught.value)


def test_settings_hide_dsn_and_bound_resources() -> None:
    assert "private-marker" not in repr(DatabaseSettings(dsn=DSN))
    for options in (
        {"max_size": 0},
        {"min_size": 0},
        {"max_waiting": 0},
        {"timeout_seconds": float("nan")},
        {"search_timeout_seconds": 0},
        {"search_timeout_seconds": float("inf")},
        {"search_max_waiting": 0},
    ):
        with pytest.raises(DatabaseConfigurationError):
            DatabaseSettings(dsn=DSN, **options)


def test_search_admission_override_keeps_other_database_limits(monkeypatch) -> None:
    monkeypatch.setenv("API_DB_SEARCH_TIMEOUT_SECONDS", "30")
    default = DatabaseSettings.from_environment({"SCW_POSTGRES_DSN": DSN})
    assert default.search_timeout_seconds == 10
    override = DatabaseSettings.from_environment({"SCW_POSTGRES_DSN": DSN, "API_DB_SEARCH_TIMEOUT_SECONDS": "30"})
    assert override.search_timeout_seconds == 30
    assert (override.max_size, override.max_waiting, override.timeout_seconds, override.statement_timeout_ms) == (4, 16, 2, 10_000)
    assert override.search_max_waiting == 64


@pytest.mark.parametrize("value", ["", "private-marker", "0", "-1", "nan", "inf", "30.01"])
def test_invalid_admission_override_is_bounded_and_does_not_leak(value) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseSettings.from_environment({"SCW_POSTGRES_DSN": DSN, "API_DB_SEARCH_TIMEOUT_SECONDS": value})
    assert str(caught.value) == "database_configuration_error"
    assert "private-marker" not in repr(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("variable", ["PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGSERVICEFILE"])
async def test_ambient_libpq_target_is_refused_before_startup_and_reconnection(monkeypatch, variable) -> None:
    database = Database(DatabaseSettings(dsn=DSN))
    monkeypatch.setenv(variable, "unexpected-private-marker")
    with pytest.raises(DatabaseConfigurationError):
        await database.open()
    assert database.closed
    with pytest.raises(DatabaseConfigurationError):
        await _SafeConnection.connect(DSN)
