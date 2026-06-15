from __future__ import annotations

import pytest
from assistant_rh_shared import Config


def test_config_reads_environment_at_instantiation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://env.example.test/v1")
    monkeypatch.setenv("SCALEWAY_API_KEY", "env-key")

    config = Config()

    assert config.scaleway_base_url == "https://env.example.test/v1"
    assert config.scaleway_api_key == "env-key"


def test_config_strips_values_and_treats_blank_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "  https://env.example.test/v1  ")
    monkeypatch.setenv("SCALEWAY_API_KEY", "   ")

    config = Config()

    assert config.scaleway_base_url == "https://env.example.test/v1"
    assert config.scaleway_api_key is None


def test_config_is_a_snapshot_of_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)

    config = Config()
    assert config.scaleway_base_url is None
    assert config.scaleway_api_key is None

    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://late.example.test/v1")
    assert config.scaleway_base_url is None
    assert Config().scaleway_base_url == "https://late.example.test/v1"
