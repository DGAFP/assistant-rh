"""Deferred chat exits. One persisted response evaluation evaluates the chat."""

import uuid
from collections.abc import MutableMapping
from typing import Any

from .chatbot_sources import is_negative_response

PENDING_EXIT = "pending_chat_exit"


def feedback_target(turns):
    """Return the last evaluable response, unless any response is already rated."""
    if any(turn.feedback for turn in turns):
        return None
    return next((turn for turn in reversed(turns) if turn.assistant.strip() and not is_negative_response(turn.assistant)), None)


def cancel_exit(state: MutableMapping[str, Any]) -> None:
    state.pop(PENDING_EXIT, None)
    if state.get("selected_ministry"):
        state["selected_ministry_picker"] = state["selected_ministry"]


def request_exit(state: MutableMapping[str, Any], ministry: str | None = None) -> None:
    """Stage an exit; the picker always reflects the still-active ministry."""
    target = feedback_target(state.get("turns", []))
    cancel_exit(state)
    state[PENDING_EXIT] = {"ministry": ministry, "turn_id": target.id if target else None, "evaluate": False, "ready": target is None}


def apply_ready_exit(state: MutableMapping[str, Any]) -> bool:
    """Run before rendering widgets, only after explicit skip or saved feedback."""
    pending = state.get(PENDING_EXIT)
    if not pending:
        return False
    saved = pending["evaluate"] and any(turn.id == pending["turn_id"] and turn.feedback for turn in state.get("turns", []))
    if not pending["ready"] and not saved:
        # Streamlit removes widget state when a widget is not rendered. Keep
        # the draft while the reminder temporarily hides the target's form.
        # Reassigning these keys detaches them from widget cleanup, without
        # retaining transient button values or restoring stale field values.
        tid = pending["turn_id"]
        fields = {f"feedback_stars_{tid}", f"comment_{tid}", f"c_{tid}"}
        prefixes = (f"pos_{tid}_", f"neg_{tid}_", f"r_{tid}_")
        for key in list(state):
            if key in fields or key.startswith(prefixes):
                state[key] = state[key]
        return False
    if pending["ministry"] is not None:
        state["selected_ministry"] = pending["ministry"]
        state["selected_ministry_picker"] = pending["ministry"]
    state["turns"] = []
    state["conversation_id"] = str(uuid.uuid4())[:8]
    state.pop("suggestions", None)
    state.pop(PENDING_EXIT, None)
    return True
