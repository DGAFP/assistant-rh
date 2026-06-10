from __future__ import annotations

import pytest
from assistant_rh_shared.config import get_env, get_scaleway_api_key, get_scaleway_base_url


def test_get_env_prefers_override_then_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "env-value")

    assert get_env("TEST_KEY", override="explicit-value") == "explicit-value"
    assert get_env("TEST_KEY") == "env-value"


def test_get_env_strips_values_and_ignores_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "  env-value  ")
    assert get_env("TEST_KEY", override="   ") == "env-value"

    monkeypatch.setenv("TEST_KEY", "   ")
    assert get_env("TEST_KEY") is None

    monkeypatch.delenv("TEST_KEY", raising=False)
    assert get_env("TEST_KEY") is None


def test_scaleway_accessors_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://env.example.test/v1")
    monkeypatch.setenv("SCALEWAY_API_KEY", "env-key")

    assert get_scaleway_base_url() == "https://env.example.test/v1"
    assert get_scaleway_base_url(override="https://explicit.example.test/v1") == "https://explicit.example.test/v1"
    assert get_scaleway_api_key() == "env-key"

    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)
    assert get_scaleway_base_url() is None
    assert get_scaleway_api_key() is None
