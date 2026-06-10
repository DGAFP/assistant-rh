"""
Shared configuration and database utilities.

Provides common dependencies for both rag-pipeline and data-engineering:
- Database connection string (get_dsn)
- SQLAlchemy engine creation (create_engine_from_env)
- Centralized environment accessors (get_env and named getters)

This package is intentionally lightweight to avoid circular dependencies.
"""

from .config import get_env, get_scaleway_api_key, get_scaleway_base_url
from .db_helpers import create_engine_from_env, get_dsn

__all__ = [
    "get_dsn",
    "create_engine_from_env",
    "get_env",
    "get_scaleway_api_key",
    "get_scaleway_base_url",
]
