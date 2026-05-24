from __future__ import annotations

from pathlib import Path

import pytest

from src.ui.cookies_security import resolve_cookies_password


@pytest.mark.parametrize("strict_var", ["DYNO", "SCALINGO_POSTGRESQL_URL"])
def test_missing_password_fails_in_production_like_env(monkeypatch, strict_var):
    monkeypatch.delenv("COOKIES_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES_PASSWORD", raising=False)
    monkeypatch.setenv(strict_var, "1")

    with pytest.raises(RuntimeError, match="COOKIES_PASSWORD"):
        resolve_cookies_password()


@pytest.mark.parametrize("env_name", ["production", "prod", "staging"])
def test_missing_password_fails_with_explicit_env_name(monkeypatch, env_name):
    monkeypatch.delenv("COOKIES_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES_PASSWORD", raising=False)
    monkeypatch.setenv("APP_ENV", env_name)

    with pytest.raises(RuntimeError, match="COOKIES_PASSWORD"):
        resolve_cookies_password()


def test_returns_configured_password(monkeypatch):
    monkeypatch.setenv("COOKIES_PASSWORD", "  strong-secret  ")
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES_PASSWORD", raising=False)

    assert resolve_cookies_password() == "strong-secret"


def test_missing_password_fails_in_dev_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("COOKIES_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES_PASSWORD", raising=False)
    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.delenv("SCALINGO_POSTGRESQL_URL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)

    with pytest.raises(RuntimeError, match="ALLOW_INSECURE_COOKIES_PASSWORD"):
        resolve_cookies_password()


def test_missing_password_allows_explicit_dev_opt_in_with_warning(monkeypatch):
    monkeypatch.delenv("COOKIES_PASSWORD", raising=False)
    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.delenv("SCALINGO_POSTGRESQL_URL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_COOKIES_PASSWORD", "true")

    with pytest.warns(RuntimeWarning, match="insecure local cookie password"):
        value = resolve_cookies_password()

    assert value == "__INSECURE_DEV_ONLY_COOKIE_PASSWORD__"


def test_whitespace_password_treated_as_missing(monkeypatch):
    monkeypatch.setenv("COOKIES_PASSWORD", "   ")
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="COOKIES_PASSWORD"):
        resolve_cookies_password()


def test_no_changeme_cookie_fallback_in_runtime_code():
    files = [
        Path("src/ui/admin_auth.py"),
        Path("apps/streamlit-ui/pages/01_Chatbot.py"),
    ]

    for file in files:
        content = file.read_text(encoding="utf-8")
        assert 'os.getenv("COOKIES_PASSWORD", "changeme")' not in content
