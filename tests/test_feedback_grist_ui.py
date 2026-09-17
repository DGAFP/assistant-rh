"""Exercise actual Streamlit interactions with synthetic data and no network."""

from pathlib import Path

import pandas as pd
import pytest
from assistant_rh_rag_pipeline.feedback_grist import FeedbackGristClient, FeedbackSyncResult
from streamlit.testing.v1 import AppTest


@pytest.fixture
def configured(monkeypatch):
    for name, value in {
        "GRIST_API_BASE_URL": "https://grist.example.test",
        "GRIST_API_KEY": "key",
        "GRIST_FEEDBACK_DOC_ID": "feedback-doc",
        "GRIST_FEEDBACK_TABLE_ID": "Feedbacks",
        "APP_SCALEWAY_ENV": "staging",
    }.items():
        monkeypatch.setenv(name, value)
    calls = []

    def sync(self, data, environment):
        calls.append((data.copy(), environment))
        return FeedbackSyncResult(total=len(data), synced=len(data))

    monkeypatch.setattr(FeedbackGristClient, "sync", sync)
    return calls


def app():
    return AppTest.from_string(
        """
import pandas as pd
from src.ui.feedback_grist import render_feedback_grist_sync
raw = pd.DataFrame([{'id': 1, 'question': 'visible', 'user_group': None}, {'id': 2, 'question': 'hidden'}])
filtered = raw.iloc[:1].copy()
filtered['user_group'] = 'unknown'
render_feedback_grist_sync(filtered, raw, 'Tout — période du dashboard')
"""
    ).run()


def test_manual_trigger_uses_raw_selected_rows_and_preserves_result(configured):
    at = app()
    assert not at.exception
    assert configured == []
    at.button(key="fb_grist_sync").click().run()
    assert not at.exception
    data, environment = configured[0]
    assert data["id"].tolist() == [1]
    assert pd.isna(data.iloc[0]["user_group"])
    assert environment == "staging"
    assert "1/1" in at.success[0].value
    at.run()
    assert len(configured) == 1
    assert "1/1" in at.success[0].value


def test_full_history_includes_hidden_and_orphan_feedbacks(configured):
    at = app()
    at.radio(key="fb_grist_scope").set_value("Tout l’historique").run()
    assert not configured
    at.button(key="fb_grist_sync").click().run()
    assert not at.exception
    assert configured[0][0]["id"].tolist() == [1, 2]
    assert "2/2" in at.success[0].value


def test_error_is_visible_and_same_button_retries(configured, monkeypatch):
    def failing(self, data, environment):
        return FeedbackSyncResult(total=1, synced=0, error="Grist : HTTP 503")

    at = app()
    original = FeedbackGristClient.sync
    monkeypatch.setattr(FeedbackGristClient, "sync", failing)
    at.button(key="fb_grist_sync").click().run()
    assert "503" in at.error[0].value
    assert "0/1" in at.error[0].value
    monkeypatch.setattr(FeedbackGristClient, "sync", original)
    at.button(key="fb_grist_sync").click().run()
    assert not at.error
    assert "1/1" in at.success[0].value


def test_missing_destination_leaves_dashboard_usable(configured, monkeypatch):
    monkeypatch.delenv("GRIST_FEEDBACK_DOC_ID")
    at = app()
    assert not at.exception
    assert not at.button
    assert "GRIST_FEEDBACK_DOC_ID" in at.info[0].value
    assert not configured


@pytest.mark.parametrize("has_feedback", [True, False])
def test_actual_dashboard_when_one_source_table_is_empty(configured, monkeypatch, has_feedback):
    import streamlit as st

    import src.ui.admin_auth as auth
    import src.ui.db_utils as db
    import src.ui.user_groups_store as groups

    st.cache_data.clear()
    monkeypatch.setattr(auth, "require_admin", lambda: None)
    monkeypatch.setattr(auth, "show_admin_badge", lambda: None)
    monkeypatch.setattr(db, "get_engine", lambda: object())
    monkeypatch.setattr(groups, "group_chart_maps", lambda: ({}, {}))
    monkeypatch.setattr(groups, "list_groups", lambda: [])

    def read_sql(query, engine):
        is_feedback = "FROM chat_feedbacks" in query
        if is_feedback == has_feedback:
            return pd.DataFrame([{"id": 99, "ts": "2026-09-15T08:00:00Z", "question": "Orphan", "answer": "Full answer", "user_group": None}])
        return pd.DataFrame()

    monkeypatch.setattr(pd, "read_sql", read_sql)
    path = Path(__file__).resolve().parents[1] / "apps/streamlit-ui/pages/03_Feedback_Dashboard.py"
    at = AppTest.from_file(str(path), default_timeout=15).run()
    assert not at.exception
    at.radio(key="fb_grist_scope").set_value("Tout l’historique").run()
    if has_feedback:
        at.button(key="fb_grist_sync").click().run()
        assert not at.exception
        assert configured[0][0]["id"].tolist() == [99]
    else:
        assert at.button(key="fb_grist_sync").disabled
        assert not configured
    st.cache_data.clear()
