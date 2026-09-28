"""Scaleway job provisioning uses runtime configuration and safe output."""

import importlib.util
from pathlib import Path

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
        "GRIST_API_BASE_URL": "https://grist.example.test",
        "GRIST_API_KEY": "grist-secret",
        "GRIST_FEEDBACK_DOC_ID": "doc-id",
        "GRIST_FEEDBACK_TABLE_ID": "Feedbacks",
        "SCW_SECRET_KEY": "scw-secret",
        "SCW_DEFAULT_PROJECT_ID": "project-id",
    }.items():
        monkeypatch.setenv(key, value)
    return deploy_job.job_settings("registry/image:commit", "2026-08-21", "*/15 * * * *")


def test_configuration_is_limited_to_job_needs(configured):
    env = configured["environment_variables"]
    assert env["APP_SCALEWAY_ENV"] == "production"
    assert env["GRIST_FEEDBACK_SINCE"] == "2026-08-21T00:00:00+02:00"
    assert env["SCW_POSTGRES_DSN"] == "postgresql://secret-dsn"
    assert "SCW_SECRET_KEY" not in env
    assert "ALBERT_API_KEY" not in env
    assert configured["cron_schedule"] == {"schedule": "*/15 * * * *", "timezone": "Europe/Paris"}


@pytest.mark.parametrize("existing", [True, False])
def test_upsert_does_not_duplicate_job_and_starts_only_on_request(configured, monkeypatch, existing):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return Response({"job_definitions": [{"name": configured["name"], "id": "job-1"}] if existing else []})
        if url.endswith("/start"):
            return Response({"job_runs": [{"id": "run-1"}]})
        return Response({"id": "job-1", "environment_variables": configured["environment_variables"]})

    monkeypatch.setattr(deploy_job.requests.Session, "request", request)
    report = deploy_job.deploy(configured, start=True)
    assert calls[1][0] == ("PATCH" if existing else "POST")
    assert len(calls) == 3
    assert report["run_ids"] == ["run-1"]
    assert "secret" not in str(report)


def test_ambiguous_duplicate_jobs_are_not_modified(configured, monkeypatch):
    calls = []

    def request(self, method, url, **kwargs):
        calls.append(method)
        from unittest.mock import Mock

        return Mock(status_code=200, json=lambda: {"job_definitions": [{"name": configured["name"], "id": str(i)} for i in range(2)]})

    monkeypatch.setattr(deploy_job.requests.Session, "request", request)
    with pytest.raises(RuntimeError, match="Plusieurs jobs"):
        deploy_job.deploy(configured)
    assert calls == ["GET"]
