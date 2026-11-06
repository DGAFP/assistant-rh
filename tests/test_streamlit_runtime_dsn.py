from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

import scaleway_streamlit_deploy  # noqa: E402

REQUIRED = ("ALBERT_API_KEY", "SCALEWAY_API_KEY", "COOKIES_PASSWORD", "ADMIN_PASSWORD", "GRIST_API_KEY", "SCW_ACCESS_KEY", "SCW_SECRET_KEY")


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in REQUIRED:
        monkeypatch.setenv(name, "synthetic-" + name.lower())
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://admin@db.invalid/assistant_rh")
    monkeypatch.delenv("STREAMLIT_POSTGRES_DSN", raising=False)
    return monkeypatch


def test_streamlit_runs_as_its_own_login_once_provisioned(secrets: pytest.MonkeyPatch) -> None:
    secrets.setenv("STREAMLIT_POSTGRES_DSN", "postgresql://assistant_rh_streamlit@db.invalid/assistant_rh")
    env = scaleway_streamlit_deploy.streamlit_secret_environment()
    assert env["SCW_POSTGRES_DSN"] == "postgresql://assistant_rh_streamlit@db.invalid/assistant_rh"
    assert "STREAMLIT_POSTGRES_DSN" not in env


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_runtime_login_falls_back_to_the_admin_dsn(secrets: pytest.MonkeyPatch, value: str | None) -> None:
    if value is not None:
        secrets.setenv("STREAMLIT_POSTGRES_DSN", value)
    assert scaleway_streamlit_deploy.streamlit_secret_environment()["SCW_POSTGRES_DSN"] == "postgresql://admin@db.invalid/assistant_rh"
