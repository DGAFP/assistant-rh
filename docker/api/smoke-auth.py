"""Verify the local stack over HTTP using only its public synthetic account."""

import os

import httpx

with httpx.Client(base_url=f"http://127.0.0.1:{os.getenv('API_PORT', '8000')}", timeout=10) as client:
    assert client.get("/healthz").status_code == 200
    groups = client.get("/v1/auth/groups")
    assert groups.status_code == 200
    assert any(group["slug"] == "local-demo" for group in groups.json()["data"])
    login = client.post("/v1/auth/session", json={"slug": "local-demo", "password": "local-only-password"})
    assert login.status_code == 200
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get("/v1/auth/me", headers=headers).status_code == 200
    assert client.delete("/v1/auth/session", headers=headers).status_code == 204
    assert client.get("/v1/auth/me", headers=headers).status_code == 401
print("Local auth smoke passed: health, catalogue, login, me, logout, revoked bearer")
