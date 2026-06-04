from __future__ import annotations

import os
import warnings

_TRUTHY = {"1", "true", "yes", "on"}
_STRICT_ENV_NAMES = {"staging", "production"}
_INSECURE_DEV_FALLBACK = "__INSECURE_DEV_ONLY_COOKIE_PASSWORD__"


def _is_truthy_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in _TRUTHY


def is_production_like_env() -> bool:
    """Return True when runtime should fail closed on missing secrets."""
    env_name = (
        os.getenv("APP_ENV")
        or os.getenv("ENV")
        or os.getenv("ENVIRONMENT")
        or ""
    ).strip().lower()
    return env_name in _STRICT_ENV_NAMES


def resolve_cookies_password() -> str:
    """
    Resolve the cookie encryption password with explicit fail-closed behavior.

    - production-like environments: COOKIES_PASSWORD is mandatory.
    - local/dev environments: COOKIES_PASSWORD is mandatory unless explicit
      opt-in ALLOW_INSECURE_COOKIES_PASSWORD=true is set.
    """
    password = os.getenv("COOKIES_PASSWORD", "").strip()
    if password:
        return password

    if is_production_like_env():
        raise RuntimeError(
            "Missing required COOKIES_PASSWORD in staging/production. "
            "Set a strong secret value before starting Streamlit."
        )

    if not _is_truthy_env("ALLOW_INSECURE_COOKIES_PASSWORD"):
        raise RuntimeError(
            "Missing COOKIES_PASSWORD. In local/dev, either set COOKIES_PASSWORD "
            "or explicitly opt in with ALLOW_INSECURE_COOKIES_PASSWORD=true."
        )

    warnings.warn(
        "Using insecure local cookie password fallback because "
        "ALLOW_INSECURE_COOKIES_PASSWORD=true. Do not use this in staging/production.",
        RuntimeWarning,
        stacklevel=2,
    )
    return _INSECURE_DEV_FALLBACK
