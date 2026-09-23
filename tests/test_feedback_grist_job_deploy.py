"""Scaleway job provisioning uses runtime configuration and safe output."""

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "deploy_feedback_grist_job", Path(__file__).resolve().parents[1] / "scripts/deploy_feedback_grist_job.py"
)
deploy_job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_job)


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "APP_SCALEWAY_ENV": "production",
        "APP_DB_TARGET": "scaleway",
        "SCW_POSTGRES_DSN": "postgresql://secret-dsn",
        "SCW_POSTGRES_DSN_SECRET_ID": "11111111-1111-4111-8111-111111111111",
        "GRIST_API_BASE_URL": "https://grist.example.test",
        "GRIST_API_KEY": "grist-secret",
        "GRIST_API_KEY_SECRET_ID": "22222222-2222-4222-8222-222222222222",
        "GRIST_FEEDBACK_DOC_ID": "doc-id",
        "GRIST_FEEDBACK_TABLE_ID": "Feedbacks",
        "SCW_SECRET_KEY": "scw-secret",
        "SCW_DEFAULT_PROJECT_ID": "project-id",
    }.items():
        monkeypatch.setenv(key, value)
    for name in deploy_job.SECRET_NAMES:
        monkeypatch.delenv(f"{name}_SECRET_VERSION", raising=False)
    return deploy_job.job_settings("registry/image:commit", "2026-08-21", "*/15 * * * *")


def test_configuration_is_limited_to_job_needs(configured):
    env = configured["environment_variables"]
    assert env["APP_SCALEWAY_ENV"] == "production"
    assert env["GRIST_FEEDBACK_SINCE"] == "2026-08-21T00:00:00+02:00"
    assert "SCW_POSTGRES_DSN" not in env
    assert "GRIST_API_KEY" not in env
    assert "SCW_SECRET_KEY" not in env
    assert "ALBERT_API_KEY" not in env
    assert configured["cron_schedule"] == {"schedule": "*/15 * * * *", "timezone": "Europe/Paris"}


class JobsServer:
    """Stateful API double: definitions expose env values, references expose IDs."""

    def __init__(self, settings, existing=False):
        self.definition = {**deepcopy(settings), "id": "job-1"} if existing else None
        if self.definition:
            self.definition["environment_variables"].update(SCW_POSTGRES_DSN="postgresql://secret-dsn", GRIST_API_KEY="grist-secret")
        self.references = []
        self.calls = []
        self.runs = 0
        self.fail_reference = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        payload = kwargs.get("json", {})
        if url.endswith("/job-definitions"):
            if method == "GET":
                return Mock(status_code=200, json=lambda: {"job_definitions": [deepcopy(self.definition)] if self.definition else []})
            assert method == "POST"
            assert self.definition is None, "Deployment must reuse the existing job"
            assert "cron_schedule" not in payload, "Do not activate a new cron before attaching secrets"
            self.definition = {**deepcopy(payload), "id": "job-1"}
            result = self.definition
        elif url.endswith("/secrets"):
            if method == "GET":
                return Mock(status_code=200, json=lambda: {"secrets": deepcopy(self.references)})
            assert method == "POST"
            if self.fail_reference:
                return Mock(status_code=403)
            for ref in payload["secrets"]:
                self.references.append(
                    {
                        "secret_id": f"ref-{len(self.references)}",
                        "env_var": {"name": ref["env_var_name"]},
                        "secret_manager_id": ref["secret_manager_id"],
                        "secret_manager_version": ref["secret_manager_version"],
                    }
                )
            result = {"secrets": self.references}
        elif "/secrets/" in url:
            assert method == "PATCH"
            result = next(ref for ref in self.references if url.endswith("/" + ref["secret_id"]))
            result.update(payload)
        elif url.endswith("/start"):
            assert method == "POST"
            self.runs += 1
            result = {"job_runs": [{"id": "run-1"}]}
        else:
            assert method == "PATCH" and url.endswith("/job-definitions/job-1")
            assert {ref["env_var"]["name"] for ref in self.references} == set(deploy_job.SECRET_NAMES)
            self.definition.update(deepcopy(payload))
            result = self.definition
        return Mock(status_code=200, json=lambda: deepcopy(result))


def install_server(monkeypatch, server):
    monkeypatch.setattr(deploy_job.requests.Session, "request", lambda self, *args, **kwargs: server.request(*args, **kwargs))


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("start", [True, False])
def test_upsert_does_not_duplicate_job_and_starts_only_on_request(configured, monkeypatch, existing, start):
    server = JobsServer(configured, existing)
    install_server(monkeypatch, server)
    for _ in range(2):
        report = deploy_job.deploy(configured, deploy_job.job_secrets(), start=start)
        assert report.get("run_ids") == (["run-1"] if start else None)
        assert "secret" not in str(report)
    creates = [call for call in server.calls if call[0] == "POST" and call[1].endswith("/job-definitions")]
    assert len(creates) == (0 if existing else 1)
    assert server.runs == (2 if start else 0)
    assert len(server.references) == 2
    assert server.definition["environment_variables"] == configured["environment_variables"]
    assert server.definition["cron_schedule"] == configured["cron_schedule"]
    assert "postgresql://secret-dsn" not in str(server.calls)
    assert "grist-secret" not in str(server.calls)


def test_secret_version_rotation_preserves_references(configured, monkeypatch):
    server = JobsServer(configured)
    install_server(monkeypatch, server)
    deploy_job.deploy(configured, deploy_job.job_secrets())
    monkeypatch.setenv("GRIST_API_KEY_SECRET_VERSION", "2")
    deploy_job.deploy(configured, deploy_job.job_secrets())
    assert len(server.references) == 2
    assert server.references[1]["secret_manager_version"] == "2"
    assert server.references[1]["secret_id"] == "ref-1"


@pytest.mark.parametrize("duplicate", [True, False])
def test_conflicting_secret_references_are_not_modified(configured, monkeypatch, duplicate):
    server = JobsServer(configured)
    install_server(monkeypatch, server)
    deploy_job.deploy(configured, deploy_job.job_secrets())
    if duplicate:
        server.references.append(deepcopy(server.references[0]))
    else:
        monkeypatch.setenv("SCW_POSTGRES_DSN_SECRET_ID", "33333333-3333-4333-8333-333333333333")
    server.calls.clear()
    with pytest.raises(RuntimeError, match="Référence Secret Manager"):
        deploy_job.deploy(configured, deploy_job.job_secrets(), start=True)
    assert all(method == "GET" for method, _, _ in server.calls)
    assert server.runs == 0


def test_reference_failure_does_not_activate_or_start_new_job(configured, monkeypatch):
    server = JobsServer(configured)
    server.fail_reference = True
    install_server(monkeypatch, server)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        deploy_job.deploy(configured, deploy_job.job_secrets(), start=True)
    assert "cron_schedule" not in server.definition
    assert server.runs == 0
    server.fail_reference = False
    deploy_job.deploy(configured, deploy_job.job_secrets(), start=True)
    assert len(server.references) == 2
    assert server.runs == 1


def test_retry_after_reference_timeout_does_not_duplicate_secrets(configured, monkeypatch):
    server = JobsServer(configured)
    install_server(monkeypatch, server)
    request = server.request

    def timeout_after_create(method, url, **kwargs):
        result = request(method, url, **kwargs)
        if method == "POST" and url.endswith("/secrets"):
            raise deploy_job.requests.Timeout("private API response")
        return result

    monkeypatch.setattr(server, "request", timeout_after_create)
    with pytest.raises(RuntimeError, match="erreur réseau"):
        deploy_job.deploy(configured, deploy_job.job_secrets(), start=True)
    assert "cron_schedule" not in server.definition
    assert server.runs == 0
    monkeypatch.setattr(server, "request", request)
    deploy_job.deploy(configured, deploy_job.job_secrets(), start=True)
    assert len(server.references) == 2
    assert server.runs == 1


def test_remaining_plaintext_credentials_fail_without_starting(configured, monkeypatch, capsys):
    server = JobsServer(configured, existing=True)
    install_server(monkeypatch, server)
    request = server.request

    def stale_environment(method, url, **kwargs):
        result = request(method, url, **kwargs)
        if method == "PATCH" and url.endswith("/job-definitions/job-1"):
            server.definition["environment_variables"]["GRIST_API_KEY"] = "grist-secret"
        return result

    monkeypatch.setattr(server, "request", stale_environment)
    monkeypatch.setattr(sys, "argv", ["deploy", "--image", "registry/image:commit", "--since", "2026-08-21", "--start"])
    assert deploy_job.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "error"
    assert "grist-secret" not in output
    assert server.runs == 0


def test_deployment_needs_no_plaintext_credentials(configured, monkeypatch):
    for name in deploy_job.SECRET_NAMES:
        monkeypatch.delenv(name)
    assert deploy_job.job_settings("registry/image:commit", "2026-08-21", "*/15 * * * *") == configured
    assert {ref["env_var_name"] for ref in deploy_job.job_secrets()} == set(deploy_job.SECRET_NAMES)


def test_dry_run_does_not_call_scaleway_or_print_credentials(configured, monkeypatch, capsys):
    request = Mock(side_effect=AssertionError("No network in dry-run"))
    monkeypatch.setattr(deploy_job.requests.Session, "request", request)
    monkeypatch.setattr(sys, "argv", ["deploy", "--image", "registry/image:commit", "--since", "2026-08-21", "--dry-run"])
    assert deploy_job.main() == 0
    output = capsys.readouterr().out
    assert len(json.loads(output)["secret_references"]) == 2
    for credential in ("postgresql://secret-dsn", "grist-secret", "scw-secret"):
        assert credential not in output
    request.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "invalid-id"])
def test_missing_or_invalid_secret_id_fails_before_network(configured, monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("GRIST_API_KEY_SECRET_ID")
    else:
        monkeypatch.setenv("GRIST_API_KEY_SECRET_ID", value)
    request = Mock()
    monkeypatch.setattr(deploy_job.requests.Session, "request", request)
    monkeypatch.setattr(sys, "argv", ["deploy", "--image", "registry/image:commit", "--since", "2026-08-21", "--start"])
    assert deploy_job.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    request.assert_not_called()


def test_ambiguous_duplicate_jobs_are_not_modified(configured, monkeypatch):
    calls = []

    def request(self, method, url, **kwargs):
        calls.append(method)
        return Mock(status_code=200, json=lambda: {"job_definitions": [{"name": configured["name"], "id": str(i)} for i in range(2)]})

    monkeypatch.setattr(deploy_job.requests.Session, "request", request)
    with pytest.raises(RuntimeError, match="Plusieurs jobs"):
        deploy_job.deploy(configured, deploy_job.job_secrets())
    assert calls == ["GET"]
