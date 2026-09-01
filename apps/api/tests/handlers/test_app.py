from __future__ import annotations

import httpx
import pytest
from assistant_rh_api.core.health import HealthReport
from assistant_rh_api.handlers.app import create_app


class FakeHealthProbe:
    def __init__(self, report: HealthReport) -> None:
        self.report = report
        self.calls = 0

    async def check(self) -> HealthReport:
        self.calls += 1
        return self.report


@pytest.mark.anyio
async def test_healthz_returns_ok_without_rag_or_provider_initialization() -> None:
    probe = FakeHealthProbe(HealthReport(db="ok", config_loaded=True))
    transport = httpx.ASGITransport(app=create_app(health_probe=probe))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "config_loaded": True}
    assert probe.calls == 1


@pytest.mark.anyio
async def test_healthz_returns_503_when_runtime_is_not_ready() -> None:
    probe = FakeHealthProbe(HealthReport(db="ok", config_loaded=False))
    transport = httpx.ASGITransport(app=create_app(health_probe=probe))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "ok", "config_loaded": False}


def test_application_factory_does_not_open_a_database_connection(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("application creation must not open Postgres")

    monkeypatch.setattr("assistant_rh_api.db.health.psycopg.AsyncConnection.connect", fail_if_called)

    create_app()
