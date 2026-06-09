from __future__ import annotations

from pathlib import Path

import pytest
from assistant_rh_shared.config import resolve_config_value, resolve_config_value_candidates


def test_resolve_config_value_prefers_explicit_then_env_then_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("TEST_KEY=dotenv-value\n", encoding="utf-8")

    monkeypatch.setenv("TEST_KEY", "env-value")

    assert resolve_config_value("TEST_KEY", explicit_value="explicit-value", env_path=env_path) == "explicit-value"
    assert resolve_config_value("TEST_KEY", env_path=env_path) == "env-value"

    monkeypatch.delenv("TEST_KEY")
    assert resolve_config_value("TEST_KEY", env_path=env_path) == "dotenv-value"


def test_resolve_config_value_candidates_uses_python_dotenv_effective_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TEST_KEY=dotenv-first\nTEST_KEY=dotenv-second\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_KEY", "env-value")

    assert resolve_config_value_candidates("TEST_KEY", explicit_value="explicit-value", env_path=env_path) == [
        "explicit-value",
        "env-value",
        "dotenv-second",
    ]


def test_resolve_config_value_required_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_KEY", raising=False)

    with pytest.raises(RuntimeError, match="custom missing"):
        resolve_config_value(
            "TEST_KEY",
            env_path=tmp_path / ".env",
            required=True,
            missing_message="custom missing",
        )
