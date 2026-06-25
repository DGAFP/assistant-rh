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


def test_streamlit_deploy_rejects_enabled_tracing_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_trace_env(monkeypatch)
    monkeypatch.setenv("RAG_TRACING_ENABLED", "true")

    with pytest.raises(RuntimeError, match="no OTLP endpoint"):
        scaleway_streamlit_deploy.streamlit_runtime_environment("staging")


def test_streamlit_deploy_passes_otlp_headers_as_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    required = {
        "SCW_POSTGRES_DSN": "postgresql://db",
        "ALBERT_API_KEY": "albert",
        "SCALEWAY_API_KEY": "scaleway",
        "COOKIES_PASSWORD": "cookies",
        "ADMIN_PASSWORD": "admin",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer token",
    }
    for key, value in required.items():
        monkeypatch.setenv(key, value)

    env = scaleway_streamlit_deploy.streamlit_secret_environment()

    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer token"


def test_streamlit_workflows_expose_trace_export_configuration() -> None:
    staging = (REPO_ROOT / ".github/workflows/streamlit-deploy-staging.yml").read_text(encoding="utf-8")
    production = (REPO_ROOT / ".github/workflows/streamlit-deploy-production.yml").read_text(encoding="utf-8")

    for workflow in (staging, production):
        assert "RAG_TRACING_ENABLED" in workflow
        assert "OTEL_SERVICE_NAME" in workflow
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in workflow
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in workflow
        assert "OTEL_EXPORTER_OTLP_HEADERS" in workflow


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


def test_rag_trace_dashboard_workflow_imports_expected_dashboard() -> None:
    workflow = (REPO_ROOT / ".github/workflows/rag-trace-dashboard-deploy.yml").read_text(encoding="utf-8")

    assert "config/grafana/rag-trace-explorer-dashboard.json" in workflow
    assert "grafana_dashboard_import.py" in workflow
    assert "COCKPIT_GRAFANA_URL" in workflow
    assert "COCKPIT_GRAFANA_API_TOKEN" in workflow
    assert "RAG_TRACE_GRAFANA_FOLDER_UID" in workflow
