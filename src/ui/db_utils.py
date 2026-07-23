"""
Thin Streamlit wrapper around assistant_rh_rag_pipeline.db_helpers.

This module delegates to the pipeline package for the actual engine creation,
and applies Streamlit's @st.cache_resource for caching.

This keeps Streamlit coupling in one place (this file), while the pipeline
package remains usable in scripts, tests, and future APIs.
"""

from __future__ import annotations

import logging

import streamlit as st

logger = logging.getLogger(__name__)


@st.cache_resource
def get_engine():
    """Return a shared SQLAlchemy engine, or *None* if no DB is available.

    Safe to call repeatedly — the engine is cached via ``st.cache_resource``.
    """
    from assistant_rh_rag_pipeline.db_helpers import create_engine_from_env

    return create_engine_from_env()


def missing_tables(engine, names: list[str]) -> list[str]:
    """Return the subset of *names* absent from the connected database.

    Lets multi-environment pages degrade cleanly (message + feature off)
    instead of crashing on a table that only exists in some environments.
    Fails closed: if introspection errors, every name is reported missing.
    """
    try:
        from sqlalchemy import inspect

        existing = set(inspect(engine).get_table_names())
    except Exception:
        return list(names)
    return [name for name in names if name not in existing]
