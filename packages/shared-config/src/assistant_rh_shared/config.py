"""Shared environment configuration.

Centralizes the `os.getenv` reads so orchestration and business code do
not access the environment directly. Loading `.env` files stays the
responsibility of entrypoints (e.g. `dotenv.load_dotenv`); `Config`
snapshots the process environment at instantiation time.
"""

from __future__ import annotations

import os


def _read_env(key_name: str) -> str | None:
    value = os.getenv(key_name, "").strip()
    return value or None


class Config:
    """Environment values shared across packages, read at instantiation.

    Scope grows progressively as `os.getenv` calls migrate here from the
    rest of the codebase.
    """

    def __init__(self) -> None:
        self.scaleway_base_url = _read_env("SCALEWAY_BASE_URL")
        self.scaleway_api_key = _read_env("SCALEWAY_API_KEY")
