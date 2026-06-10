"""Shared environment configuration accessors.

Centralizes `os.getenv` reads so orchestration and business code do not
access the environment directly. Loading `.env` files stays the
responsibility of entrypoints (e.g. `dotenv.load_dotenv`); this module
only reads the process environment.
"""

from __future__ import annotations

import os

SCALEWAY_BASE_URL_ENV = "SCALEWAY_BASE_URL"
SCALEWAY_API_KEY_ENV = "SCALEWAY_API_KEY"


def get_env(key_name: str, *, override: str | None = None) -> str | None:
    """Return the explicit override if non-empty, else the environment value.

    Values are stripped; blank values are treated as missing.
    """
    for candidate in (override, os.getenv(key_name)):
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return None


def get_scaleway_base_url(override: str | None = None) -> str | None:
    return get_env(SCALEWAY_BASE_URL_ENV, override=override)


def get_scaleway_api_key() -> str | None:
    return get_env(SCALEWAY_API_KEY_ENV)
