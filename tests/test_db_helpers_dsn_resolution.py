from __future__ import annotations

import pytest
from assistant_rh_rag_pipeline import db_helpers
from assistant_rh_shared import db_helpers as shared_db_helpers
from sqlalchemy.exc import SQLAlchemyError


def _clear_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_DB_TARGET",
        "APP_POSTGRES_DSN",
        "STREAMLIT_POSTGRES_DSN",
        "SCW_POSTGRES_DSN",
        "SCALINGO_POSTGRESQL_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_scalingo_target_is_no_longer_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scalingo")
    monkeypatch.setenv("SCALINGO_POSTGRESQL_URL", "postgresql://deprecated")

    with pytest.raises(RuntimeError, match="expected: scaleway"):
        db_helpers.get_dsn()


def test_scalingo_dsn_is_not_used_as_implicit_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("SCALINGO_POSTGRESQL_URL", "postgresql://deprecated")

    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        db_helpers.get_dsn()


def test_scaleway_target_uses_canonical_dsn_without_environment_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://scw")

    assert db_helpers.get_dsn() == "postgresql://scw"


def test_scaleway_target_requires_canonical_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")

    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        db_helpers.get_dsn()


def test_no_target_uses_scaleway_dsn_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://legacy")

    assert db_helpers.get_dsn() == "postgresql://legacy"


def test_shared_config_rejects_scalingo_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scalingo")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://scw")
    monkeypatch.setenv("SCALINGO_POSTGRESQL_URL", "postgresql://deprecated")

    with pytest.raises(RuntimeError, match="expected: scaleway"):
        shared_db_helpers.get_dsn()


def test_shared_config_does_not_use_scalingo_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("SCALINGO_POSTGRESQL_URL", "postgresql://deprecated")

    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        shared_db_helpers.get_dsn()


def test_shared_config_scaleway_target_uses_canonical_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://scw")

    assert shared_db_helpers.get_dsn() == "postgresql://scw"


def test_dsn_fallbacks_ignore_whitespace_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("SCW_POSTGRES_DSN", "   ")
    monkeypatch.setenv("APP_POSTGRES_DSN", "  postgresql://app  ")

    assert db_helpers.get_dsn() == "postgresql://app"
    assert shared_db_helpers.get_dsn() == "postgresql://app"


def test_scaleway_target_rejects_whitespace_canonical_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dsn_env(monkeypatch)
    monkeypatch.setenv("APP_DB_TARGET", "scaleway")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "   ")

    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        db_helpers.get_dsn()
    with pytest.raises(RuntimeError, match="SCW_POSTGRES_DSN"):
        shared_db_helpers.get_dsn()


def test_prompt_db_failure_falls_back_to_local_prompt(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class FailingCursor:
        def __enter__(self) -> "FailingCursor":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def execute(self, *args: object, **kwargs: object) -> None:
            raise db_helpers.psycopg.OperationalError("prompt db unavailable")

    class FailingConnection:
        def cursor(self) -> FailingCursor:
            return FailingCursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(db_helpers, "_db_conn", lambda: FailingConnection())

    with caplog.at_level("DEBUG"):
        content = db_helpers.get_prompt_content("generator.md")

    assert content is not None
    assert "Prompt DB lookup failed" in caplog.text


def test_prompt_list_db_failure_falls_back_to_local_prompts(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class FailingCursor:
        def __enter__(self) -> "FailingCursor":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def execute(self, *args: object, **kwargs: object) -> None:
            raise db_helpers.psycopg.OperationalError("prompt db unavailable")

    class FailingConnection:
        def cursor(self) -> FailingCursor:
            return FailingCursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(db_helpers, "_db_conn", lambda: FailingConnection())

    with caplog.at_level("DEBUG"):
        prompts = db_helpers.list_prompts("generator")

    assert "generator.md" in prompts
    assert "Prompt DB listing failed" in caplog.text


def test_rag_engine_creation_returns_none_for_sqlalchemy_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://db.example.test/app")
    monkeypatch.setattr(db_helpers, "get_sqlalchemy_url", lambda: "postgresql+psycopg://db.example.test/app")

    def fail_create_engine(*args: object, **kwargs: object) -> object:
        raise SQLAlchemyError("cannot create engine")

    monkeypatch.setattr("sqlalchemy.create_engine", fail_create_engine)

    with caplog.at_level("WARNING"):
        assert db_helpers.create_engine_from_env() is None

    assert "DB engine creation failed" in caplog.text


def test_shared_engine_creation_returns_none_for_sqlalchemy_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://db.example.test/app")

    def fail_create_engine(*args: object, **kwargs: object) -> object:
        raise SQLAlchemyError("cannot create engine")

    monkeypatch.setattr("sqlalchemy.create_engine", fail_create_engine)

    with caplog.at_level("WARNING"):
        assert shared_db_helpers.create_engine_from_env() is None

    assert "DB engine creation failed" in caplog.text
