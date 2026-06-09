from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def resolve_config_value_candidates(
    key_name: str,
    *,
    explicit_value: str | None = None,
    env_path: Path | None = None,
) -> list[str]:
    """Resolve config values from explicit override, environment, then dotenv file."""
    values: list[str] = []

    for candidate in (
        explicit_value,
        os.getenv(key_name),
        _dotenv_value(env_path, key_name),
    ):
        value = _clean_value(candidate)
        if value and value not in values:
            values.append(value)

    return values


def resolve_config_value(
    key_name: str,
    *,
    explicit_value: str | None = None,
    env_path: Path | None = None,
    required: bool = False,
    missing_message: str | None = None,
) -> str | None:
    """Resolve a single config value from explicit override, environment, then dotenv file."""
    candidates = resolve_config_value_candidates(key_name, explicit_value=explicit_value, env_path=env_path)
    if candidates:
        return candidates[0]
    if required:
        raise RuntimeError(missing_message or f"{key_name} manquant.")
    return None


def _dotenv_value(env_path: Path | None, key_name: str) -> str | None:
    if env_path is None or not env_path.exists():
        return None
    value = dotenv_values(env_path).get(key_name)
    return value if isinstance(value, str) else None


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
