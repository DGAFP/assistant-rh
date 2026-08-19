from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import grafana_dashboard_import  # noqa: E402
import scaleway_streamlit_deploy  # noqa: E402


def _clear_trace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RAG_TRACING_ENABLED",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


def _streamlit_required_secret_env() -> dict[str, str]:
    return {
        "SCW_POSTGRES_DSN": "postgresql://db",
        "ALBERT_API_KEY": "albert",
        "SCALEWAY_API_KEY": "scaleway",
        "COOKIES_PASSWORD": "cookies",
        "ADMIN_PASSWORD": "admin",
        "GROUP_DEFAULT_PASSWORD": "groups",
        "GRIST_API_KEY": "grist",
        "SCW_ACCESS_KEY": "access",
        "SCW_SECRET_KEY": "secret",
    }


def test_streamlit_deploy_passes_otlp_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_trace_env(monkeypatch)
    monkeypatch.setenv("RAG_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "assistant-rh")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://tempo.example/v1/traces")

    env = scaleway_streamlit_deploy.streamlit_runtime_environment("staging")

    assert env["RAG_TRACING_ENABLED"] == "true"
    assert env["OTEL_SERVICE_NAME"] == "assistant-rh"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "https://tempo.example/v1/traces"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env


def test_streamlit_deploy_passes_source_import_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCW_DEFAULT_REGION", "fr-par")
    monkeypatch.setenv("SCW_BUCKET_SOURCES_PDF", "assistant-rh-sources-pdf-staging")
    monkeypatch.setenv("GRIST_API_BASE_URL", "https://grist.example")
    monkeypatch.setenv("GRIST_DOC_ID", "doc123")
    monkeypatch.setenv("GRIST_TABLE_ID", "Sources")

    env = scaleway_streamlit_deploy.streamlit_runtime_environment("staging")

    assert env["SCW_DEFAULT_REGION"] == "fr-par"
    assert env["SCW_BUCKET_SOURCES_PDF"] == "assistant-rh-sources-pdf-staging"
    assert env["GRIST_API_BASE_URL"] == "https://grist.example"
    assert env["GRIST_DOC_ID"] == "doc123"
    assert env["GRIST_TABLE_ID"] == "Sources"


def test_streamlit_deploy_rejects_enabled_tracing_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_trace_env(monkeypatch)
    monkeypatch.setenv("RAG_TRACING_ENABLED", "true")

    with pytest.raises(RuntimeError, match="no OTLP endpoint"):
        scaleway_streamlit_deploy.streamlit_runtime_environment("staging")


def test_streamlit_deploy_passes_otlp_headers_as_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    required = _streamlit_required_secret_env()
    required["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=Bearer token"
    for key, value in required.items():
        monkeypatch.setenv(key, value)

    env = scaleway_streamlit_deploy.streamlit_secret_environment()

    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer token"
    assert env["GRIST_API_KEY"] == "grist"
    assert env["GROUP_DEFAULT_PASSWORD"] == "groups"
    assert env["SCW_ACCESS_KEY"] == "access"
    assert env["SCW_SECRET_KEY"] == "secret"


def test_streamlit_deploy_requires_source_import_secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    required = _streamlit_required_secret_env()
    required.pop("GRIST_API_KEY")
    for key in _streamlit_required_secret_env():
        monkeypatch.delenv(key, raising=False)
    for key, value in required.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="GRIST_API_KEY"):
        scaleway_streamlit_deploy.streamlit_secret_environment()


def test_streamlit_workflows_expose_trace_export_configuration() -> None:
    staging = (REPO_ROOT / ".github/workflows/streamlit-deploy-staging.yml").read_text(encoding="utf-8")
    production = (REPO_ROOT / ".github/workflows/streamlit-deploy-production.yml").read_text(encoding="utf-8")

    for workflow in (staging, production):
        assert "RAG_TRACING_ENABLED" in workflow
        assert "OTEL_SERVICE_NAME" in workflow
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in workflow
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in workflow
        assert "OTEL_EXPORTER_OTLP_HEADERS" in workflow


def test_streamlit_workflows_expose_source_import_configuration() -> None:
    staging = (REPO_ROOT / ".github/workflows/streamlit-deploy-staging.yml").read_text(encoding="utf-8")
    production = (REPO_ROOT / ".github/workflows/streamlit-deploy-production.yml").read_text(encoding="utf-8")

    for workflow in (staging, production):
        assert "GRIST_API_BASE_URL" in workflow
        assert "GRIST_API_KEY" in workflow
        assert "GRIST_DOC_ID" in workflow
        assert "GRIST_TABLE_ID" in workflow
        assert "SCW_BUCKET_SOURCES_PDF" in workflow
        assert "GROUP_DEFAULT_PASSWORD" in workflow


def test_grafana_import_payload_requires_stable_dashboard_uid() -> None:
    dashboard = json.loads((REPO_ROOT / "config/grafana/rag-trace-explorer-dashboard.json").read_text(encoding="utf-8"))

    payload = grafana_dashboard_import.build_payload(dashboard, folder_uid="rag", message="import")

    assert payload["dashboard"]["uid"] == "assistant-rh-rag-trace-explorer"
    assert payload["folderUid"] == "rag"
    assert payload["overwrite"] is True
    assert payload["message"] == "import"


def test_grafana_import_payload_rejects_uidless_dashboard() -> None:
    with pytest.raises(ValueError, match="stable uid"):
        grafana_dashboard_import.build_payload({"title": "no uid"})


def test_grafana_import_auth_headers_support_bearer_and_basic() -> None:
    assert grafana_dashboard_import.auth_headers(api_token="grafana-token") == {"Authorization": "Bearer grafana-token"}
    assert grafana_dashboard_import.auth_headers(basic_auth="user:password") == {"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}


def test_grafana_import_auth_headers_require_credentials() -> None:
    with pytest.raises(RuntimeError, match="GRAFANA_API_TOKEN or GRAFANA_BASIC_AUTH"):
        grafana_dashboard_import.auth_headers()


def test_grafana_import_reports_non_json_success_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<html>Bad Gateway</html>"

    def urlopen(*_args: object, **_kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(grafana_dashboard_import.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="non-JSON response with HTTP 200"):
        grafana_dashboard_import.import_dashboard(
            grafana_url="https://grafana.example",
            api_token="token",
            payload={"dashboard": {"uid": "assistant-rh-rag-trace-explorer"}, "overwrite": True},
        )


def test_rag_trace_dashboard_workflow_imports_expected_dashboard() -> None:
    workflow = (REPO_ROOT / ".github/workflows/rag-trace-dashboard-deploy.yml").read_text(encoding="utf-8")

    assert "config/grafana/rag-trace-explorer-dashboard.json" in workflow
    assert "grafana_dashboard_import.py" in workflow
    assert "COCKPIT_GRAFANA_URL" in workflow
    assert "COCKPIT_GRAFANA_API_TOKEN" in workflow
    assert "COCKPIT_GRAFANA_BASIC_AUTH" in workflow
    assert "GRAFANA_API_TOKEN or GRAFANA_BASIC_AUTH" in workflow
    assert "RAG_TRACE_GRAFANA_FOLDER_UID" in workflow
