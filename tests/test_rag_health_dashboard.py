from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = REPO_ROOT / "config" / "grafana" / "rag-health-dashboard.json"


def _dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def test_dashboard_env_selector_includes_staging_and_prod_without_metric_bootstrap() -> None:
    variables = _dashboard()["templating"]["list"]
    env_variable = next(variable for variable in variables if variable["name"] == "env")

    assert env_variable["type"] == "custom"
    assert env_variable["includeAll"] is True
    assert env_variable["allValue"] == "staging|prod"
    assert env_variable["query"] == "staging,prod"
    assert {option["value"] for option in env_variable["options"]} == {"staging", "prod"}


def test_dashboard_queries_use_shared_env_selector() -> None:
    dashboard = _dashboard()
    expressions = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", []) if target.get("expr")]

    assert expressions
    assert all('env=~"$env"' in expression for expression in expressions)


def test_dashboard_declares_prometheus_datasource_variable() -> None:
    variables = _dashboard()["templating"]["list"]
    datasource_variable = next(variable for variable in variables if variable["name"] == "datasource")

    assert datasource_variable["type"] == "datasource"
    assert datasource_variable["query"] == "prometheus"
    assert datasource_variable["includeAll"] is False


def test_dashboard_panels_use_prometheus_datasource_variable() -> None:
    dashboard = _dashboard()
    datasources = [panel["datasource"] for panel in dashboard["panels"]]

    assert datasources
    assert all(datasource["type"] == "prometheus" for datasource in datasources)
    assert all(datasource["uid"] == "$datasource" for datasource in datasources)
    assert all(not datasource["uid"].startswith("${") for datasource in datasources)
