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
    for options in ({"max_size": 0}, {"min_size": 0}, {"max_waiting": 0}, {"timeout_seconds": float("nan")}):
        with pytest.raises(DatabaseConfigurationError):
            DatabaseSettings(dsn=DSN, **options)


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
