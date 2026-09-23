"""Shared source query for the dashboard and scheduled Grist reconciliation."""

FEEDBACK_SELECT = """
SELECT
    f.id,
    f.ts,
    f.turn_id,
    f.turn_idx,
    f.helpful,
    f.reasons,
    f.reasons_positive,
    f.reasons_negative,
    f.comment,
    f.stars,
    f.session_id,
    COALESCE(NULLIF(f.question, ''), r.question) AS question,
    COALESCE(NULLIF(f.answer, ''), r.answer) AS answer,
    f.error_category,
    f.ai_reason,
    f.ai_analyzed_at,
    f.beta_scope,
    COALESCE(r.v3_detected_theme, f.theme) as theme,
    r.user_group,
    r.selected_ministry,
    r.dist_after_rerank,
    r.rag_version,
    r.chunk_selection_mode,
    r.total_time_ms
FROM chat_feedbacks f
LEFT JOIN chat_runs r ON f.turn_id = r.turn_id
"""
