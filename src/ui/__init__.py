"""
UI utilities for Streamlit pages.
"""
from .chatbot_feedback import (
    is_feedback_pending,
    render_feedback_block,
    render_feedback_block_v1,
    render_feedback_block_v2,
)
from .chatbot_llm import (
    get_fallback_config,
    get_llm_client,
    parse_sources_line,
    stream_and_filter_sources,
    stream_with_fallback,
)
from .chatbot_logging import (
    FEEDS_FIELDS,
    REVIEWS_FIELDS,
    RUNS_FIELDS,
    Turn,
    annotate_original_order,
    log_feedback_row,
    log_run_row,
    safe_round,
    turn_index_by_id,
    upsert_reviews,
)
from .chatbot_sources import (
    detect_source_type,
    format_source_date,
    is_negative_response,
    render_sources,
    serialize_retrieved,
    should_hide_sources,
)

# Chatbot components (extracted from 01_Chatbot.py)
from .chatbot_styles import (
    CHATBOT_CSS,
    DSFR_COLORS,
    inject_chatbot_styles,
    render_dsfr_header,
    render_welcome_message,
    source_badge_html,
)
from .db_utils import get_engine
from .llm_selector import llm_endpoint_selector

__all__ = [
    # Original exports
    "llm_endpoint_selector",
    "get_engine",
    # Styles
    "inject_chatbot_styles",
    "render_dsfr_header",
    "render_welcome_message",
    "source_badge_html",
    "DSFR_COLORS",
    "CHATBOT_CSS",
    # Sources
    "render_sources",
    "format_source_date",
    "detect_source_type",
    "is_negative_response",
    "should_hide_sources",
    "serialize_retrieved",
    # Logging
    "Turn",
    "log_run_row",
    "log_feedback_row",
    "upsert_reviews",
    "turn_index_by_id",
    "safe_round",
    "annotate_original_order",
    "RUNS_FIELDS",
    "FEEDS_FIELDS",
    "REVIEWS_FIELDS",
    # Feedback
    "render_feedback_block",
    "render_feedback_block_v1",
    "render_feedback_block_v2",
    "is_feedback_pending",
    # LLM
    "get_llm_client",
    "get_fallback_config",
    "stream_with_fallback",
    "stream_and_filter_sources",
    "parse_sources_line",
]

