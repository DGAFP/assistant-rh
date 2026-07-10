"""Tests transactionnels du seed versionné du prompt selector."""

from __future__ import annotations

from copy import deepcopy

import pytest
from assistant_rh_rag_pipeline import db_helpers

from scripts import seed_selector_prompt as script


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._many: list[tuple] = []
        self._one: tuple | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params: tuple | None = None) -> None:
        normalized = " ".join(query.split())
        upper = normalized.upper()
        params = tuple(params or ())
        self.conn.statements.append((normalized, params))
        self._many = []
        self._one = None

        if upper.startswith("SELECT NAME, CONTENT, DESCRIPTION, PROMPT_TYPE, IS_ACTIVE"):
            names = set(params)
            self._many = [
                (
                    name,
                    row["content"],
                    row["description"],
                    row["prompt_type"],
                    row["is_active"],
                )
                for name, row in self.conn._tx_prompts.items()
                if name in names
            ]
            return

        if upper.startswith("SELECT CONFIG ->>"):
            if self.conn._tx_config is not None:
                self._one = (self.conn._tx_config.get(params[0]),)
            return

        if upper.startswith("INSERT INTO SYSTEM_PROMPTS"):
            name, content, description, prompt_type, updated_by = params
            self.conn._tx_prompts[name] = {
                "content": content,
                "description": description,
                "prompt_type": prompt_type,
                "is_active": True,
                "updated_by": updated_by,
            }
            return

        if upper.startswith("UPDATE RAG_CONFIG"):
            pointer_key, prompt_name, json_updated_by, table_updated_by, returned_key = params
            assert pointer_key == returned_key
            assert json_updated_by == table_updated_by
            if self.conn.force_missing_update or self.conn._tx_config is None:
                return
            self.conn._tx_config[pointer_key] = prompt_name
            self.conn._tx_config["updated_at"] = "db-now"
            self.conn._tx_config["updated_by"] = json_updated_by
            self._one = (self.conn._tx_config[returned_key],)
            return

        raise AssertionError(f"Unexpected SQL: {normalized}")

    def fetchall(self) -> list[tuple]:
        return list(self._many)

    def fetchone(self) -> tuple | None:
        return self._one


class FakeConnection:
    def __init__(
        self,
        *,
        prompts: dict[str, dict[str, object]] | None = None,
        config: dict[str, object] | None = None,
        force_missing_update: bool = False,
    ) -> None:
        self.prompts = deepcopy(prompts or {})
        self.config = deepcopy(config)
        self._tx_prompts = deepcopy(self.prompts)
        self._tx_config = deepcopy(self.config)
        self.force_missing_update = force_missing_update
        self.statements: list[tuple[str, tuple]] = []
        self.cursor_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        self.cursor_calls += 1
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.prompts = deepcopy(self._tx_prompts)
        self.config = deepcopy(self._tx_config)

    def rollback(self) -> None:
        self.rollbacks += 1
        self._tx_prompts = deepcopy(self.prompts)
        self._tx_config = deepcopy(self.config)

    def close(self) -> None:
        self.closed = True


def _old_prompt() -> dict[str, object]:
    return {
        "content": "ancien prompt\n",
        "description": "v1",
        "prompt_type": script.PROMPT_TYPE,
        "is_active": True,
    }


def _current_v2(content: str) -> dict[str, object]:
    return {
        "content": content,
        "description": script.DESCRIPTION,
        "prompt_type": script.PROMPT_TYPE,
        "is_active": True,
    }


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path, conn: FakeConnection, argv: list[str]) -> tuple[int, int]:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "selector.md").write_text("nouveau prompt\n", encoding="utf-8")

    connection_calls = 0

    def get_conn() -> FakeConnection:
        nonlocal connection_calls
        connection_calls += 1
        return conn

    monkeypatch.setattr(db_helpers, "_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(db_helpers, "has_dsn", lambda: True)
    monkeypatch.setattr(db_helpers, "_db_conn", get_conn)
    return script.main(argv), connection_calls


def _dml(conn: FakeConnection) -> list[tuple[str, tuple]]:
    return [statement for statement in conn.statements if statement[0].upper().startswith(("INSERT", "UPDATE"))]


def test_dry_run_performs_no_write(monkeypatch, tmp_path, capsys) -> None:
    config = {"keep": {"nested": 1}, script.POINTER_KEY: script.OLD_PROMPT_NAME}
    conn = FakeConnection(prompts={script.OLD_PROMPT_NAME: _old_prompt()}, config=config)

    result, connection_calls = _run(monkeypatch, tmp_path, conn, [])

    assert result == 0
    assert connection_calls == 1
    assert _dml(conn) == []
    assert conn.commits == 0
    assert conn.config == config
    assert script.PROMPT_NAME not in conn.prompts
    assert "Dry-run only" in capsys.readouterr().out


def test_apply_repairs_inactive_prompt_without_touching_runtime_config(monkeypatch, tmp_path, capsys) -> None:
    content = "nouveau prompt\n"
    stale_v2 = {
        "content": content,
        "description": "ancienne description",
        "prompt_type": "generator",
        "is_active": False,
    }
    config = {"keep": [1, 2], script.POINTER_KEY: script.OLD_PROMPT_NAME}
    conn = FakeConnection(prompts={script.PROMPT_NAME: stale_v2}, config=config)

    result, _ = _run(monkeypatch, tmp_path, conn, ["--apply", "--updated-by", "test"])

    assert result == 0
    assert conn.commits == 1
    assert conn.config == config
    assert conn.prompts[script.PROMPT_NAME] == {**_current_v2(content), "updated_by": "test"}
    assert len(_dml(conn)) == 1
    sql, _ = _dml(conn)[0]
    assert "is_active" in sql
    assert "is_active = TRUE" in sql
    assert "métadonnées ou état actif à réparer" in capsys.readouterr().out


def test_activate_is_atomic_and_preserves_other_config_keys(monkeypatch, tmp_path) -> None:
    config = {
        "keep": {"nested": 1},
        "v3_initial_top_k": 17,
        script.POINTER_KEY: script.OLD_PROMPT_NAME,
        "updated_at": "before",
        "updated_by": "before",
    }
    conn = FakeConnection(prompts={script.OLD_PROMPT_NAME: _old_prompt()}, config=config)

    result, connection_calls = _run(monkeypatch, tmp_path, conn, ["--activate", "--updated-by", "test"])

    assert result == 0
    assert connection_calls == 1
    assert conn.cursor_calls == 1
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.config["keep"] == {"nested": 1}
    assert conn.config["v3_initial_top_k"] == 17
    assert conn.config[script.POINTER_KEY] == script.PROMPT_NAME
    assert conn.config["updated_at"] == "db-now"
    assert conn.config["updated_by"] == "test"
    assert conn.prompts[script.PROMPT_NAME]["is_active"] is True

    pointer_select = next(sql for sql, _ in conn.statements if sql.upper().startswith("SELECT CONFIG ->>"))
    assert pointer_select.endswith("FOR UPDATE")
    runtime_update = next(sql for sql, _ in conn.statements if sql.upper().startswith("UPDATE RAG_CONFIG"))
    assert "config = config || jsonb_build_object" in runtime_update
    assert "WHERE id = 1" in runtime_update
    assert "RETURNING" in runtime_update
    assert "SET config = %s" not in runtime_update


@pytest.mark.parametrize("argv", [[], ["--apply"], ["--activate"]])
def test_missing_runtime_config_row_fails_before_writes(monkeypatch, tmp_path, capsys, argv) -> None:
    conn = FakeConnection(prompts={script.OLD_PROMPT_NAME: _old_prompt()}, config=None)

    result, _ = _run(monkeypatch, tmp_path, conn, argv)

    assert result == 1
    assert _dml(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert "rag_config id=1" in capsys.readouterr().err


def test_activate_is_dml_idempotent_when_prompt_and_pointer_are_current(monkeypatch, tmp_path, capsys) -> None:
    content = "nouveau prompt\n"
    conn = FakeConnection(
        prompts={script.PROMPT_NAME: _current_v2(content)},
        config={script.POINTER_KEY: script.PROMPT_NAME, "keep": True},
    )

    result, _ = _run(monkeypatch, tmp_path, conn, ["--activate"])

    assert result == 0
    assert _dml(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 0
    assert "Dry-run only" not in capsys.readouterr().out


def test_failed_activation_rolls_back_prompt_upsert(monkeypatch, tmp_path, capsys) -> None:
    config = {script.POINTER_KEY: script.OLD_PROMPT_NAME, "keep": True}
    conn = FakeConnection(
        prompts={script.OLD_PROMPT_NAME: _old_prompt()},
        config=config,
        force_missing_update=True,
    )

    result, _ = _run(monkeypatch, tmp_path, conn, ["--activate"])

    assert result == 1
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.config == config
    assert script.PROMPT_NAME not in conn.prompts
    assert "bascule atomique" in capsys.readouterr().err
