from __future__ import annotations

import pytest

from src.ui import admin_auth
from src.ui.user_groups_store import UserGroupsInitResult


class _SessionState(dict):
    def __getattr__(self, key: str):
        return self[key]

    def __setattr__(self, key: str, value) -> None:
        self[key] = value


def test_initialize_admin_security_invalidates_auth_when_default_admin_is_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _SessionState(
        admin_authenticated=True,
        admin_auth_method="group",
        _is_admin_cache={"group": "default", "value": True},
    )
    monkeypatch.setattr(admin_auth.st, "session_state", state)
    monkeypatch.setattr(
        admin_auth,
        "init_user_groups_table_with_status",
        lambda: UserGroupsInitResult(initialized=True, default_admin_repaired=True),
    )

    admin_auth.initialize_admin_security()

    assert "admin_authenticated" not in state
    assert "admin_auth_method" not in state
    assert "_is_admin_cache" not in state
    assert state["_admin_security_version"] == admin_auth._ADMIN_SECURITY_VERSION


def test_is_admin_rechecks_legacy_cached_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _SessionState(admin_authenticated=True, user_group="default")
    monkeypatch.setattr(admin_auth.st, "session_state", state)
    monkeypatch.setattr(admin_auth, "initialize_admin_security", lambda: None)
    monkeypatch.setattr(admin_auth, "is_admin_group", lambda _group: False)

    assert admin_auth.is_admin() is False
    assert "admin_authenticated" not in state
    assert state["_is_admin_cache"] == {"group": "default", "value": False}


def test_is_admin_trusts_password_authentication_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _SessionState(admin_authenticated=True, admin_auth_method="password")
    monkeypatch.setattr(admin_auth.st, "session_state", state)
    monkeypatch.setattr(admin_auth, "initialize_admin_security", lambda: None)
    monkeypatch.setattr(admin_auth, "is_admin_group", lambda _group: pytest.fail("database lookup must not run"))

    assert admin_auth.is_admin() is True
