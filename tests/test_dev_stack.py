"""Guards for the staging-to-local development workflow; no network or Docker required."""

import subprocess
from types import SimpleNamespace

import pytest

from scripts import dev


@pytest.fixture
def stack(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
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
        {"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@shared.invalid/db", "SCW_POSTGRES_DSN_PROD": "host=Shared.invalid port=5432 dbname=db user=x"},
        {"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@stage.invalid/db", "SCW_POSTGRES_DSN_PROD": "host=prod.invalid port=not-a-port"},
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


def test_dsn_timeout_and_application_name_are_honored():
    environment = dev.dump_environment(
        dev.source_dsn({"SCW_POSTGRES_DSN_STAGING": "postgresql://u:p@stage.invalid/db?connect_timeout=60&application_name=me"})
    )
    assert environment["PGCONNECT_TIMEOUT"] == "60"
    assert environment["PGAPPNAME"] == "me"


def test_caller_cannot_redirect_local_compose(stack, monkeypatch):
    calls = []
    monkeypatch.setenv("COMPOSE_FILE", "/wrong.yml")
    monkeypatch.setenv("PGHOST", "production.invalid")
    monkeypatch.setenv("DOCKER_CONFIG", "/home/me/.docker-alt")
    stack = dev.Stack({})
    monkeypatch.setattr(dev, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    stack.compose("ps")
    args, kwargs = calls[0]
    assert args[:4] == ["docker", "--host", "unix:///var/run/docker.sock", "compose"]
    assert "COMPOSE_FILE" not in kwargs["env"] and "PGHOST" not in kwargs["env"] and "DOCKER_HOST" not in kwargs["env"]
    assert kwargs["env"]["DOCKER_CONFIG"] == "/home/me/.docker-alt"
    assert "--project-name" in args and stack.project in args
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    with pytest.raises(ValueError):
        dev.Stack({}).compose("ps")


@pytest.mark.parametrize(("endpoint", "accepted"), [("unix:///Users/me/.colima/default/docker.sock", True), ("ssh://remote.invalid", False)])
def test_current_docker_context_is_used_only_when_local(stack, monkeypatch, endpoint, accepted):
    monkeypatch.delenv("DOCKER_HOST")
    monkeypatch.setattr(dev, "run", lambda args, **kwargs: SimpleNamespace(stdout=endpoint + "\n"))
    stack = dev.Stack({})
    if accepted:
        assert stack.daemon() == endpoint
    else:
        with pytest.raises(ValueError):
            stack.daemon()


def test_signing_key_is_generated_without_host_openssl(stack, monkeypatch):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=b"PEM\n" if "genpkey" in args else bytes(12) + bytes(range(32)))

    monkeypatch.setattr(stack, "docker", docker)
    stack.prepare()
    assert all(args[args.index("--entrypoint") + 1 : args.index("--entrypoint") + 3] == ("openssl", dev.PG_IMAGE) for args in calls)
    assert (dev.STATE / "signing.pem").stat().st_mode & 0o777 == 0o600
    assert '"x": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"' in (dev.STATE / "api.env").read_text()
    stack.prepare()
    assert len(calls) == 2
    assert dev.PG_IMAGE in (dev.ROOT / "docker/dev/compose.yml").read_text()


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


def test_failed_swap_keeps_previous_copy_and_reopens_connections(stack, monkeypatch):
    queries = []

    def sql(database, query):
        queries.append(query)
        if query == dev.SWAP_SQL:
            raise subprocess.CalledProcessError(1, ["psql"])

    monkeypatch.setattr(stack, "sql", sql)
    with pytest.raises(subprocess.CalledProcessError):
        stack.swap()
    assert queries.index(dev.SWAP_SQL) > next(i for i, query in enumerate(queries) if "ALLOW_CONNECTIONS false" in query)
    assert queries[-1] == dev.REOPEN_SQL
    assert not any("DROP DATABASE IF EXISTS assistant_rh_previous" in query for query in queries)


def test_restore_omits_only_known_legacy_fk():
    toc = (
        "1; 2606 100 FK CONSTRAINT public rag_documents rag_documents_legacy_doc_id_fkey owner\n"
        "2; 2606 101 FK CONSTRAINT public rag_sections rag_sections_doc_id_fkey owner\n"
        "3; 2606 102 FK CONSTRAINT public rag_sections rag_sections_parent_section_id_fkey owner\n"
    )
    result = dev.restore_list(toc)
    assert result == "\n".join(toc.splitlines()[1:]) + "\n"


def test_existing_fork_is_never_checked_out_or_reset(stack, monkeypatch, tmp_path):
    stack.values["CONVERSATIONS_DIR"] = str(tmp_path)
    stack.fork = tmp_path
    calls = []
    monkeypatch.setattr(dev, "run", lambda *args, **kwargs: pytest.fail("must not modify the existing checkout"))
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match="fetch"):
        stack.checkout()
    assert calls == [["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", dev.FORK_COMMIT, "HEAD"]]


def test_managed_fork_fetches_pin_absent_from_cloned_branches(stack, monkeypatch, tmp_path):
    stack.fork = tmp_path / "conversations"
    pinned, changes = [], []

    def inspect(args, **kwargs):
        return SimpleNamespace(returncode=0 if pinned else 1)

    monkeypatch.setattr(subprocess, "run", inspect)
    monkeypatch.setattr(dev, "run", lambda args, **kwargs: changes.append([str(arg) for arg in args]) or ("checkout" in args and pinned.append(1)))
    stack.checkout()
    fork = str(stack.fork)
    assert changes == [
        ["git", "clone", "--no-checkout", dev.FORK_URL, fork],
        ["git", "-C", fork, "fetch", "--quiet", dev.FORK_URL, dev.FORK_COMMIT],
        ["git", "-C", fork, "checkout", "--quiet", "--detach", dev.FORK_COMMIT],
    ]


def test_normal_start_preserves_dependencies_and_rebuild_renews_them(stack, monkeypatch):
    calls = []
    monkeypatch.setattr(stack, "checkout", lambda: None)
    monkeypatch.setattr(stack, "compose", lambda *args: calls.append(args))
    stack.dev()
    stack.dev(rebuild=True)
    assert "--build" not in calls[0] and "--renew-anon-volumes" not in calls[0]
    assert "--build" in calls[1] and "--renew-anon-volumes" in calls[1]
