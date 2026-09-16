"""Exit state and actual Streamlit callback/dialog regression coverage (no services)."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src.ui.chatbot_transitions import PENDING_EXIT, apply_ready_exit, cancel_exit, feedback_target, request_exit


def turn(answer="Une réponse", feedback=None, tid="answer-1"):
    return SimpleNamespace(id=tid, assistant=answer, user="Question", feedback=feedback)


@pytest.mark.parametrize(
    "turns",
    [[], [turn("")], [turn("  ")], [turn(feedback={"stars": 0})], [turn(feedback={"rating": "up"}), turn(feedback={"stars": 4})]],
)
def test_no_reminder(turns):
    state = {"turns": turns, "selected_ministry": "matte", "conversation_id": "original"}
    assert feedback_target(turns) is None
    request_exit(state)
    assert apply_ready_exit(state)
    assert state["turns"] == []
    assert state["conversation_id"] != "original"
    assert not apply_ready_exit(state)


def test_target_last_evaluable_response():
    first, last = turn(tid="first"), turn(tid="last")
    assert feedback_target([first, last, turn("")]) is last


@pytest.mark.parametrize("rated_feedback", [{"stars": 0}, {"stars": 4}, {"rating": "up"}, {"rating": "down"}])
@pytest.mark.parametrize("rated_last", [False, True])
def test_rated_response_does_not_hide_an_unrated_response(rated_feedback, rated_last):
    rated = turn(feedback=rated_feedback, tid="rated")
    unrated = turn(tid="unrated")
    turns = [unrated, rated] if rated_last else [rated, unrated]
    assert feedback_target(turns) is unrated


@pytest.mark.parametrize("ministry", [None, "other"])
def test_cancel_or_dismiss_preserves_original_chat(ministry):
    original = [turn()]
    state = {"turns": original, "selected_ministry": "matte", "conversation_id": "original", "selected_ministry_picker": "other"}
    request_exit(state, ministry)
    assert not apply_ready_exit(state)
    cancel_exit(state)
    assert state["turns"] is original
    assert state["conversation_id"] == "original"
    assert state["selected_ministry"] == state["selected_ministry_picker"] == "matte"
    assert PENDING_EXIT not in state


def app_script():
    """Use the page's real callbacks, dialog and three exit widget declarations."""
    source = (Path(__file__).parents[1] / "apps/streamlit-ui/pages/01_Chatbot.py").read_text()
    tree = ast.parse(source)
    names = {"_request_new_chat", "_request_ministry_change", "_cancel_chat_exit", "_chat_exit_dialog"}
    definitions = "\n".join(ast.unparse(node) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names)
    widgets = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "key" and isinstance(kw.value, ast.Constant) and kw.value.value in {"new", "new_sidebar", "selected_ministry_picker"}:
                    widgets[kw.value.value] = ast.unparse(node)
    return (
        """
import streamlit as st
from src.ui.chatbot_feedback import render_feedback_block
from src.ui.chatbot_transitions import PENDING_EXIT, apply_ready_exit, cancel_exit, request_exit
"""
        + definitions
        + """
apply_ready_exit(st.session_state)
ministere_options = ["matte", "other"]
initial_ministry = st.session_state.selected_ministry
_ministry_label = str
"""
        + "\n".join(widgets.values())
        + """
if st.session_state.get(PENDING_EXIT):
    _chat_exit_dialog()
for t in st.session_state.turns:
    if st.session_state.get(PENDING_EXIT, {}).get("turn_id") != t.id:
        render_feedback_block(t)
"""
    )


@pytest.fixture
def app():
    at = AppTest.from_string(app_script())
    for key, value in dict(turns=[turn()], selected_ministry="matte", conversation_id="original", session_id="session-original").items():
        at.session_state[key] = value
    return at.run()


def start_exit(app, action):
    if action == "ministry":
        app.selectbox(key="selected_ministry_picker").select("other").run()
    else:
        app.button(key=action).click().run()
    assert not app.exception
    assert app.session_state["conversation_id"] == "original"
    assert app.session_state["selected_ministry"] == "matte"
    assert app.selectbox(key="selected_ministry_picker").value == "matte"


@pytest.mark.parametrize("action", ["new", "new_sidebar", "ministry"])
@pytest.mark.parametrize("choice", ["exit_cancel", "exit_skip", "exit_evaluate"])
def test_three_exits_and_choices(app, monkeypatch, action, choice):
    recorded = []

    def save(row):
        assert app.session_state["conversation_id"] == "original"
        assert app.session_state["selected_ministry"] == "matte"
        assert row["turn_id"] == "answer-1"
        assert row["session_id"] == "session-original"
        recorded.append(row)

    monkeypatch.setattr("src.ui.chatbot_feedback.log_feedback_row", save)
    start_exit(app, action)
    app.button(key=choice).click().run()
    if choice == "exit_evaluate":
        app.feedback[0].set_value(0).run()
        app.text_area[0].set_value("À améliorer").run()
        app.button(key="submit_answer-1").click().run()
    assert not app.exception
    assert PENDING_EXIT not in app.session_state
    if choice == "exit_cancel":
        assert app.session_state["conversation_id"] == "original"
        assert len(app.session_state["turns"]) == 1
    else:
        assert app.session_state["conversation_id"] != "original"
        assert app.session_state["turns"] == []
    expected_ministry = "other" if action == "ministry" and choice != "exit_cancel" else "matte"
    assert app.selectbox(key="selected_ministry_picker").value == expected_ministry
    assert len(recorded) == (1 if choice == "exit_evaluate" else 0)


@pytest.mark.parametrize("action", ["new", "new_sidebar", "ministry"])
@pytest.mark.parametrize("choice", ["exit_cancel", "exit_skip", "exit_evaluate"])
def test_second_unrated_response_prompts_after_first_response_was_rated(app, monkeypatch, action, choice):
    recorded = []

    def save(row):
        assert app.session_state["selected_ministry"] == "matte"
        assert app.session_state["conversation_id"] == "original"
        recorded.append(row)

    monkeypatch.setattr("src.ui.chatbot_feedback.log_feedback_row", save)
    app.feedback[0].set_value(0).run()
    app.text_area[0].set_value("Premier avis").run()
    app.button(key="submit_answer-1").click().run()
    assert not app.exception
    assert app.session_state["turns"][0].feedback["stars"] == 0

    # Simulate the next generated answer without calling a provider.
    app.session_state["turns"].append(turn(answer="Deuxième réponse", tid="answer-2"))
    # The submitted form's widgets were removed; do not resend their stale state.
    app._run()
    start_exit(app, action)
    assert app.session_state[PENDING_EXIT]["turn_id"] == "answer-2"
    assert app.get("dialog")

    app.button(key=choice).click().run()
    if choice == "exit_evaluate":
        app.feedback[0].set_value(4).run()
        app.text_area[0].set_value("Deuxième avis").run()
        app.button(key="submit_answer-2").click().run()

    assert not app.exception
    assert PENDING_EXIT not in app.session_state
    assert [row["turn_id"] for row in recorded] == (["answer-1", "answer-2"] if choice == "exit_evaluate" else ["answer-1"])
    if choice == "exit_cancel":
        assert app.session_state["conversation_id"] == "original"
        assert len(app.session_state["turns"]) == 2
        assert app.session_state["turns"][1].feedback is None
    else:
        assert app.session_state["conversation_id"] != "original"
        assert app.session_state["turns"] == []
    expected_ministry = "other" if action == "ministry" and choice != "exit_cancel" else "matte"
    assert app.session_state["selected_ministry"] == expected_ministry
    assert app.selectbox(key="selected_ministry_picker").value == expected_ministry


@pytest.mark.parametrize("recovery", ["retry", "skip"])
def test_failed_save_retains_chat_until_retry_or_explicit_skip(app, monkeypatch, recovery):
    def fail(row):
        raise OSError("Storage unavailable")

    monkeypatch.setattr("src.ui.chatbot_feedback.log_feedback_row", fail)
    start_exit(app, "ministry")
    app.button(key="exit_evaluate").click().run()
    app.feedback[0].set_value(4).run()
    app.text_area[0].set_value("Merci").run()
    app.button(key="submit_answer-1").click().run()
    assert not app.exception
    assert app.error
    assert app.session_state["turns"][0].feedback is None
    assert app.session_state["conversation_id"] == "original"
    assert app.session_state["selected_ministry"] == "matte"
    if recovery == "retry":
        monkeypatch.setattr("src.ui.chatbot_feedback.log_feedback_row", lambda row: None)
        app.button(key="submit_answer-1").click().run()
    else:
        app.button(key="exit_skip").click().run()
    assert not app.exception
    assert app.session_state["turns"] == []
    assert app.session_state["selected_ministry"] == "other"


@pytest.mark.parametrize("action", ["new", "new_sidebar", "ministry"])
@pytest.mark.parametrize("turns", [[], [turn("")], [turn(feedback={"stars": 0})]])
def test_widgets_without_reminder(app, action, turns):
    app.session_state["turns"] = turns
    app.run()
    if action == "ministry":
        app.selectbox(key="selected_ministry_picker").select("other").run()
    else:
        app.button(key=action).click().run()
    assert not app.exception
    assert PENDING_EXIT not in app.session_state
    assert app.session_state["turns"] == []
    assert app.session_state["selected_ministry"] == ("other" if action == "ministry" else "matte")


@pytest.mark.parametrize("evaluate", [False, True])
def test_dialog_dismiss_callback(app, evaluate):
    from streamlit.proto.WidgetStates_pb2 import WidgetStates

    start_exit(app, "ministry")
    if evaluate:
        app.button(key="exit_evaluate").click().run()
    dialog = app.get("dialog")[0]
    # AppTest has no dismiss convenience method; send the frontend's trigger.
    states = WidgetStates()
    states.widgets.add(id=dialog.proto.dialog.id, trigger_value=True)
    app._run(states)
    assert not app.exception
    assert PENDING_EXIT not in app.session_state
    assert app.session_state["conversation_id"] == "original"
    assert app.session_state["selected_ministry"] == "matte"
    assert app.selectbox(key="selected_ministry_picker").value == "matte"
    assert len(app.session_state["turns"]) == 1


@pytest.mark.parametrize("action", ["new", "new_sidebar", "ministry"])
@pytest.mark.parametrize("choice", ["exit_cancel", "exit_evaluate", "dismiss"])
@pytest.mark.parametrize("legacy", [False, True])
def test_reminder_preserves_unsent_feedback(app, action, choice, legacy):
    app.session_state["use_feedback_v2"] = not legacy
    app.run()
    if legacy:
        app.button(key="down_answer-1").click().run()
        reason_key, comment_key = "r_answer-1_0", "c_answer-1"
    else:
        app.feedback[0].set_value(3).run()
        reason_key, comment_key = "pos_answer-1_0", "comment_answer-1"
        app.checkbox(key="neg_answer-1_0").check().run()
    app.checkbox(key=reason_key).check().run()
    app.text_area(key=comment_key).set_value("Un avis encore en cours").run()

    start_exit(app, action)
    app.run()  # The draft must survive more than the first hidden render.
    if choice == "dismiss":
        from streamlit.proto.WidgetStates_pb2 import WidgetStates

        states = WidgetStates()
        states.widgets.add(id=app.get("dialog")[0].proto.dialog.id, trigger_value=True)
        app._run(states)
    else:
        app.button(key=choice).click().run()

    assert not app.exception
    assert app.checkbox(key=reason_key).value is True
    assert app.text_area(key=comment_key).value == "Un avis encore en cours"
    if not legacy:
        assert app.feedback[0].value == 3
        assert app.checkbox(key="neg_answer-1_0").value is True
    assert app.session_state["turns"][0].feedback is None
    assert app.session_state["conversation_id"] == "original"


@pytest.mark.parametrize("rating", ["up", "down"])
def test_legacy_feedback_is_saved_before_exit(app, monkeypatch, rating):
    recorded = []
    monkeypatch.setattr("src.ui.chatbot_feedback.log_feedback_row", recorded.append)
    app.session_state["use_feedback_v2"] = False
    start_exit(app, "new")
    app.button(key="exit_evaluate").click().run()
    app.button(key=f"{rating}_answer-1").click().run()
    if rating == "down":
        app.button(key="s_answer-1").click().run()
    assert not app.exception
    assert len(recorded) == 1
    assert recorded[0]["helpful"] == (rating == "up")
    assert app.session_state["turns"] == []
