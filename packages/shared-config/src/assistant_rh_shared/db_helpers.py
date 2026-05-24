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


def get_dsn() -> str:
    """Return the PostgreSQL connection string from environment variables."""
    dsn = os.getenv("SCALINGO_POSTGRESQL_URL") or os.getenv("PG_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("No database connection string found (set SCALINGO_POSTGRESQL_URL, PG_DSN or DATABASE_URL).")
    return dsn


def create_engine_from_env() -> Optional["Engine"]:
    """Create a SQLAlchemy engine from environment variables.

    Returns None if no DB is configured (DATABASE_URL not set) or if the
    connection fails (e.g., invalid credentials, network issues).
    Does NOT use Streamlit caching — suitable for scripts, tests, and APIs.
    """
    from sqlalchemy import create_engine, text

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
    except Exception as exc:
        logger.warning("DB engine creation failed: %s", exc)
        return None