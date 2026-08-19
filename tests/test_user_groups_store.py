from __future__ import annotations

from typing import Any

import pytest

from src.ui import user_groups_store
from src.ui.groups import ADMIN_GROUP, DEFAULT_GROUP, GROUPS


class _Cursor:
    def __init__(self, rows: list[tuple[str, str | None]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))
        self.rowcount = 1 if "SET is_admin = FALSE" in query else 0

    def fetchall(self) -> list[tuple[str, str | None]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[tuple[str, str | None]]) -> None:
        self.cursor_instance = _Cursor(rows)
        self.committed = False
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def test_init_backfills_missing_seed_passwords_and_repairs_default_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection([(group.slug, None) for group in GROUPS])
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("GROUP_DEFAULT_PASSWORD", "group-secret")
    monkeypatch.setattr(user_groups_store, "_conn", lambda: connection)
    monkeypatch.setattr(user_groups_store, "hash_password", lambda value: f"hash:{value}")

    result = user_groups_store.init_user_groups_table_with_status()

    assert result.initialized is True
    assert result.default_admin_repaired is True

    password_updates = [
        params
        for query, params in connection.cursor_instance.executed
        if "SET password_hash = %s" in query
    ]
    assert len(password_updates) == len(GROUPS)
    assert ("hash:admin-secret", ADMIN_GROUP) in password_updates
    assert all(
        password_hash == "hash:group-secret"
        for password_hash, slug in password_updates
        if slug != ADMIN_GROUP
    )
    assert any(
        "SET is_admin = FALSE" in query and params == (DEFAULT_GROUP,)
        for query, params in connection.cursor_instance.executed
    )
    assert connection.committed is True
    assert connection.closed is True


def test_init_boolean_wrapper_preserves_existing_api(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection([(group.slug, "existing") for group in GROUPS])
    monkeypatch.setattr(user_groups_store, "_conn", lambda: connection)

    assert user_groups_store.init_user_groups_table() is True


def test_update_group_rejects_default_admin_without_database_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_groups_store, "_conn", lambda: pytest.fail("database access must not occur"))

    ok, error = user_groups_store.update_group(DEFAULT_GROUP, is_admin=True)

    assert ok is False
    assert "ne peut pas être administrateur" in error
