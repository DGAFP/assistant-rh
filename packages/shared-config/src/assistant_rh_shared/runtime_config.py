from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_value_candidates(env_path: Path | None, key_name: str) -> list[str]:
    """Return non-empty values for a key in a dotenv file, preserving file order."""
    if env_path is None or not env_path.exists():
        return []

    values: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != key_name:
            continue
        candidate = value.strip().strip('"').strip("'")
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def resolve_runtime_value_candidates(
    key_name: str,
    *,
    explicit_value: str | None = None,
    env_path: Path | None = None,
) -> list[str]:
    """Resolve runtime values from explicit override, environment, then dotenv file."""
    values: list[str] = []

    if explicit_value:
        value = explicit_value.strip()
        if value:
            values.append(value)

    env_value = os.getenv(key_name, "").strip()
    if env_value and env_value not in values:
        values.append(env_value)

    for candidate in load_dotenv_value_candidates(env_path, key_name):
        if candidate not in values:
            values.append(candidate)

    return values


def resolve_runtime_value(
    key_name: str,
    *,
    explicit_value: str | None = None,
    env_path: Path | None = None,
    required: bool = False,
    missing_message: str | None = None,
) -> str | None:
    """Resolve a single runtime value from explicit override, environment, then dotenv file."""
    candidates = resolve_runtime_value_candidates(key_name, explicit_value=explicit_value, env_path=env_path)
    if candidates:
        return candidates[0]
    if required:
        raise RuntimeError(missing_message or f"{key_name} manquant.")
    return None
