from __future__ import annotations

import httpx
import pytest
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.health import PostgresHealthProbe
from assistant_rh_api.db.pool import Database
from assistant_rh_api.handlers.app import create_app


@pytest.mark.anyio
async def test_postgres_health_probe_uses_only_synthetic_database(synthetic_database_dsn: str) -> None:
    database = Database(DatabaseSettings(dsn=synthetic_database_dsn))
    application = create_app(database=database)
    transport = httpx.ASGITransport(app=application)

    async with application.router.lifespan_context(application):
        assert not database.closed
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
    assert database.closed

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "config_loaded": True}


@pytest.mark.anyio
async def test_postgres_health_probe_reports_missing_dsn(monkeypatch) -> None:
    monkeypatch.delenv("SCW_POSTGRES_DSN", raising=False)

    report = await PostgresHealthProbe().check()

    assert report.db == "error"
    assert report.config_loaded is False
    assert report.is_healthy is False
