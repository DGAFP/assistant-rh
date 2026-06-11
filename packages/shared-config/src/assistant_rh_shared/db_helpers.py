"""
Database connection utilities for shared-config.

Provides basic DB connectivity without any pipeline-specific dependencies.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SCALEWAY_DSN_ENV_KEY = "SCW_POSTGRES_DSN"

DSN_ENV_KEYS = (
    "SCW_POSTGRES_DSN",
    "APP_POSTGRES_DSN",
    "STREAMLIT_POSTGRES_DSN",
)


def get_dsn() -> str:
    """Return the PostgreSQL connection string from environment variables."""
    target = os.getenv("APP_DB_TARGET", "").strip().lower()
    if target:
        if target == "scaleway":
            dsn = os.getenv(SCALEWAY_DSN_ENV_KEY, "").strip()
            if dsn:
                return dsn

            raise RuntimeError(f"APP_DB_TARGET=scaleway requires {SCALEWAY_DSN_ENV_KEY} to be set.")

        raise RuntimeError(f"Unsupported APP_DB_TARGET={target!r} (expected: scaleway).")

    dsn = next((value for key in DSN_ENV_KEYS if (value := os.getenv(key, "").strip())), "")
    if not dsn:
        raise RuntimeError(f"No database connection string found (set one of: {', '.join(DSN_ENV_KEYS)}).")
    return dsn


def create_engine_from_env() -> Optional["Engine"]:
    """Create a SQLAlchemy engine from environment variables.

    Returns None if no DB is configured or if the
    connection fails (e.g., invalid credentials, network issues).
    Does NOT use Streamlit caching — suitable for scripts, tests, and APIs.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        url = get_dsn()
    except RuntimeError:
        return None

    # Convert postgres:// → postgresql+psycopg://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"

    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=2,
            max_overflow=1,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("DB engine creation failed: %s", exc)
        return None
