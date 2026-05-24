from __future__ import annotations

import pytest
from assistant_rh_rag_pipeline import db_helpers


def _clear_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_DB_TARGET",
        "APP_SCALEWAY_ENV",
        "APP_POSTGRES_DSN",
        "STREAMLIT_POSTGRES_DSN",
        "SCALINGO_POSTGRESQL_URL",
        "SCW_POSTGRES_DSN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_scalingo_target_requires_scalingo_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scalingo")

    with pytest.raises(RuntimeError, match="SCALINGO_POSTGRESQL_URL"):
        db_helpers.get_dsn()


def test_scaleway_prod_uses_prod_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("APP_SCALEWAY_ENV", "prod")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://prod")

    assert db_helpers.get_dsn() == "postgresql://prod"


def test_scaleway_staging_does_not_fallback_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("APP_SCALEWAY_ENV", "staging")

    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        db_helpers.get_dsn()


def test_scaleway_requires_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://prod")

    with pytest.raises(RuntimeError, match="APP_SCALEWAY_ENV"):
        db_helpers.get_dsn()


def test_no_target_uses_scaleway_dsn_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://legacy")

    assert db_helpers.get_dsn() == "postgresql://legacy"

