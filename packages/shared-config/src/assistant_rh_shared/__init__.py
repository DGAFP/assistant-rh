"""
Shared configuration and database utilities.

Provides common dependencies for both rag-pipeline and data-engineering:
- Database connection string (get_dsn)
- SQLAlchemy engine creation (create_engine_from_env)

This package is intentionally lightweight to avoid circular dependencies.
"""
from .db_helpers import create_engine_from_env, get_dsn

__all__ = ["get_dsn", "create_engine_from_env"]