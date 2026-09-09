from pathlib import Path

import httpx
import psycopg
import pytest
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database
from assistant_rh_api.handlers.app import create_app

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[4]


async def test_local_bootstrap_manifest_supports_public_auth(synthetic_database_dsn):
    # The same ordered files are consumed by Compose's init-local-db.sh.
    manifest = ROOT / "docker/api/bootstrap-local.txt"
    with psycopg.connect(synthetic_database_dsn) as connection:
        for _ in range(2):
            for filename in manifest.read_text().splitlines():
                connection.execute((ROOT / filename).read_text())
    app = create_app(database=Database(DatabaseSettings(dsn=synthetic_database_dsn)), environ={})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
            groups = await client.get("/v1/auth/groups")
            assert groups.status_code == 200
            assert any(group["slug"] == "local-demo" for group in groups.json()["data"])
            login = await client.post("/v1/auth/session", json={"slug": "local-demo", "password": "local-only-password"})
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200
            assert (await client.delete("/v1/auth/session", headers=headers)).status_code == 204
            assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401
