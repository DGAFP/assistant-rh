from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = REPO_ROOT / "config" / "grafana" / "rag-trace-explorer-dashboard.json"


def _dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def test_trace_dashboard_declares_expected_variables() -> None:
    variables = {variable["name"]: variable for variable in _dashboard()["templating"]["list"]}

    assert variables["trace_datasource"]["type"] == "datasource"
    assert variables["trace_datasource"]["query"] == "tempo"
    assert variables["metrics_datasource"]["type"] == "datasource"
    assert variables["metrics_datasource"]["query"] == "prometheus"
    assert "postgres_datasource" not in variables
    assert variables["env"]["includeAll"] is True
    assert variables["env"]["allValue"] == ".*"
    for name in ("turn_id", "trace_id", "stage", "status"):
        assert name in variables


def test_trace_dashboard_defaults_text_filters_to_match_all_regex() -> None:
    variables = {variable["name"]: variable for variable in _dashboard()["templating"]["list"]}

    assert variables["turn_id"]["current"]["value"] == ".*"
    assert variables["turn_id"]["current"]["text"] == ".*"
    assert variables["trace_id"]["current"]["value"] == ".*"
    assert variables["trace_id"]["current"]["text"] == ".*"


def test_trace_dashboard_uses_portable_datasource_uids() -> None:
    datasources = [panel["datasource"] for panel in _dashboard()["panels"] if "datasource" in panel]

    assert datasources
    assert {datasource["uid"] for datasource in datasources} == {"$trace_datasource", "$metrics_datasource"}
    assert all(not datasource["uid"].startswith("${") for datasource in datasources)


def test_trace_dashboard_contains_required_panels() -> None:
    titles = {panel["title"] for panel in _dashboard()["panels"]}

    assert "Recent RAG traces" in titles
    assert "Pipeline stage timeline" in titles
    assert "Trace events by stage and status" in titles
    assert "Stage duration p95" in titles
    assert "Errors and fallback events" in titles
    assert "Trace freshness" in titles
    assert "Admin drilldown" in titles


def test_trace_dashboard_uses_rag_health_prometheus_metrics_instead_of_postgres() -> None:
    dashboard = _dashboard()
    serialized = json.dumps(dashboard)
    prometheus_queries = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if panel.get("datasource", {}).get("type") == "prometheus"
    ]

    assert prometheus_queries
    assert any("assistant_rh_rag_trace_events_24h_total" in query for query in prometheus_queries)
    assert any("assistant_rh_rag_trace_stage_duration_seconds" in query for query in prometheus_queries)
    assert "postgres" not in serialized
    assert "rawSql" not in serialized
    assert "v3_full_prompt" not in serialized
    assert "full_prompt" not in serialized


def test_traceql_panels_filter_by_turn_and_trace() -> None:
    dashboard = _dashboard()
    traceql_queries = [
        target["query"] for panel in dashboard["panels"] for target in panel.get("targets", []) if target.get("queryType") == "traceql"
    ]

    assert traceql_queries
    assert all("span.rag.turn_id" in query for query in traceql_queries)
    assert all("span.rag.trace_id" in query for query in traceql_queries)
