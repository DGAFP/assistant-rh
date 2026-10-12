"""Guards for the staging-to-local development workflow; no network or Docker required."""

import subprocess
from types import SimpleNamespace

import pytest

from scripts import dev


@pytest.fixture
def stack(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(dev, "STATE", tmp_path)
    (tmp_path / "dumps").mkdir()
    return dev.Stack({"SCW_POSTGRES_DSN_STAGING": "postgresql://reader:secret@stage.invalid:5432/staging?sslmode=require"})


def test_env_is_data_not_shell(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export KEY='a$literal#value'\nVALUE=$(touch unexpected)\nEMPTY=\nOTHER=plain # comment\n")
    assert dev.read_env(path) == {"KEY": "a$literal#value", "VALUE": "$(touch unexpected)", "EMPTY": "", "OTHER": "plain"}
    output = tmp_path / "private.env"
    dev.write_env(output, {"KEY": "a$literal#value"})
    assert output.read_text() == "KEY=a$literal#value\n"
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "values",
    [
        {"SCW_POSTGRES_DSN": "postgresql://u:p@prod.invalid/db"},
        {"SCW_POSTGRES_DSN_STAGING": "postgresql:///missing-host"},
        {"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@stage.invalid/db?host=prod.invalid"},
        {"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@stage.invalid/db?options=-c%20default_transaction_read_only=off"},
        {"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@shared.invalid/db", "SCW_POSTGRES_DSN_PROD": "postgresql://other:p@shared.invalid:5432/db"},
    ],
)
def test_dump_requires_unambiguous_staging_source(values):
    with pytest.raises(ValueError):
        dev.source_dsn(values)


def test_source_credentials_stay_out_of_command_arguments(stack):
    environment = dev.dump_environment(dev.source_dsn(stack.values))
    assert environment["PGDATABASE"] == "staging"
    assert environment["PGPASSWORD"] == "secret"
    assert environment["PGSSLMODE"] == "require"
    assert environment["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert not any(table.startswith(("chat_", "api_", "user_")) for table in dev.TABLES)
    assert len(dev.TABLES) == 12


def test_caller_cannot_redirect_local_compose(stack, monkeypatch):
    calls = []
    monkeypatch.setenv("COMPOSE_FILE", "/wrong.yml")
    monkeypatch.setenv("PGHOST", "production.invalid")
    stack = dev.Stack({})
    monkeypatch.setattr(dev, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    stack.compose("ps")
    args, kwargs = calls[0]
    assert args[:4] == ["docker", "--context", "default", "compose"]
    assert "COMPOSE_FILE" not in kwargs["env"] and "PGHOST" not in kwargs["env"]
    assert "--project-name" in args and stack.project in args
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    with pytest.raises(ValueError):
        dev.Stack({})


def test_failed_restore_preserves_current_database_and_services(stack, monkeypatch):
    calls, queries = [], []

    def compose(*args, **kwargs):
        calls.append((args, kwargs))
        if "pg_restore" in args and "--list" not in args:
            raise subprocess.CalledProcessError(1, ["pg_restore"])
        return SimpleNamespace(stdout="api\n")

    monkeypatch.setattr(stack, "compose", compose)
    monkeypatch.setattr(stack, "sql", lambda database, sql: queries.append((database, sql)))
    with pytest.raises(subprocess.CalledProcessError):
        stack.dump()
    assert all("ALTER DATABASE" not in query for _, query in queries)
    assert all("DROP DATABASE IF EXISTS assistant_rh " not in query for _, query in queries)
    assert not any("stop" in args for args, _ in calls)
    export = next((args, kwargs) for args, kwargs in calls if "pg_dump" in args)
    assert "secret" not in str(export[0])
    assert export[1]["extra_env"]["PGOPTIONS"] == "-c default_transaction_read_only=on"
    restore = next(args for args, _ in calls if "pg_restore" in args and "--list" not in args)
    assert restore[restore.index("-h") + 1] == "/var/run/postgresql"
    assert restore[restore.index("-d") + 1] == "assistant_rh_import"


def test_restore_omits_only_known_legacy_fk():
    toc = (
        "1; 2606 100 FK CONSTRAINT public rag_documents rag_documents_legacy_doc_id_fkey owner\n"
        "2; 2606 101 FK CONSTRAINT public rag_sections rag_sections_doc_id_fkey owner\n"
        "3; 2606 102 FK CONSTRAINT public rag_sections rag_sections_parent_section_id_fkey owner\n"
    )
    result = dev.restore_list(toc)
    assert result == "\n".join(toc.splitlines()[1:]) + "\n"


def test_existing_fork_is_never_checked_out_or_reset(stack, monkeypatch, tmp_path):
    stack.fork = tmp_path
    calls = []
    monkeypatch.setattr(dev, "run", lambda *args, **kwargs: pytest.fail("must not modify the existing checkout"))
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    stack.checkout()
    assert calls == [["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", dev.FORK_COMMIT, "HEAD"]]


def test_normal_start_preserves_dependencies_and_rebuild_renews_them(stack, monkeypatch):
    calls = []
    monkeypatch.setattr(stack, "checkout", lambda: None)
    monkeypatch.setattr(stack, "compose", lambda *args: calls.append(args))
    stack.dev()
    stack.dev(rebuild=True)
    assert "--build" not in calls[0] and "--renew-anon-volumes" not in calls[0]
    assert "--build" in calls[1] and "--renew-anon-volumes" in calls[1]
