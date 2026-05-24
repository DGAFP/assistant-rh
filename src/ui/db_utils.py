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
