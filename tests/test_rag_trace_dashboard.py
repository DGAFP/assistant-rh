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
    for name in ("turn_id", "trace_id_filter", "trace_id", "stage", "status"):
        assert name in variables


def test_trace_dashboard_defaults_text_filters_to_match_all_regex() -> None:
    variables = {variable["name"]: variable for variable in _dashboard()["templating"]["list"]}

    assert variables["turn_id"]["current"]["value"] == ".*"
    assert variables["turn_id"]["current"]["text"] == ".*"
    assert variables["trace_id_filter"]["current"]["value"] == ".*"
    assert variables["trace_id_filter"]["current"]["text"] == ".*"
    assert variables["trace_id"]["current"]["value"] == "00000000000000000000000000000001"
    assert variables["trace_id"]["current"]["text"] == "00000000000000000000000000000001"


def test_trace_dashboard_uses_portable_datasource_uids() -> None:
    datasources = [panel["datasource"] for panel in _dashboard()["panels"] if "datasource" in panel]

    assert datasources
    assert {datasource["uid"] for datasource in datasources} == {"$trace_datasource", "$metrics_datasource"}
    assert all(not datasource["uid"].startswith("${") for datasource in datasources)


def test_trace_dashboard_contains_required_panels() -> None:
    titles = {panel["title"] for panel in _dashboard()["panels"]}

    assert "Recent RAG traces" in titles
    assert "Selected trace waterfall" in titles
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


def test_recent_traces_table_hides_nested_tempo_fields_and_links_to_selected_trace() -> None:
    panel = next(panel for panel in _dashboard()["panels"] if panel["title"] == "Recent RAG traces")
    transformations = {transformation["id"]: transformation["options"] for transformation in panel["transformations"]}
    organize = transformations["organize"]

    assert panel["type"] == "table"
    assert organize["excludeByName"]["spanSet"] is True
    assert organize["excludeByName"]["spanSets"] is True
    assert organize["excludeByName"]["serviceStats"] is True
    assert organize["renameByName"]["traceID"] == "Trace ID"

    trace_id_override = next(
        override
        for override in panel["fieldConfig"]["overrides"]
        if override["matcher"]["id"] == "byName" and override["matcher"]["options"] == "traceID"
    )
    links = next(property_["value"] for property_ in trace_id_override["properties"] if property_["id"] == "links")

    assert links[0]["title"] == "Show trace in this dashboard"
    assert "orgId=1" not in links[0]["url"]
    assert "var-trace_id_filter=$trace_id_filter" in links[0]["url"]
    assert "var-trace_id=${__data.fields.traceID}" in links[0]["url"]


def test_selected_trace_uses_native_traces_panel() -> None:
    panel = next(panel for panel in _dashboard()["panels"] if panel["title"] == "Selected trace waterfall")

    assert panel["type"] == "traces"
    assert panel["datasource"]["type"] == "tempo"
    assert panel["targets"][0]["query"] == "$trace_id"
    assert panel["targets"][0]["queryType"] == "traceql"


def test_selected_trace_query_does_not_use_match_all_filter_default() -> None:
    variables = {variable["name"]: variable for variable in _dashboard()["templating"]["list"]}
    selected_trace_id = variables["trace_id"]["current"]["value"]
    recent_traces_panel = next(panel for panel in _dashboard()["panels"] if panel["title"] == "Recent RAG traces")

    assert selected_trace_id != ".*"
    assert len(selected_trace_id) == 32
    assert all(character in "0123456789abcdef" for character in selected_trace_id)
    assert 'span.rag.trace_id =~ "$trace_id_filter"' in recent_traces_panel["targets"][0]["query"]


def test_traceql_panels_filter_by_turn_and_trace() -> None:
    dashboard = _dashboard()
    traceql_queries = [
        target["query"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if target.get("queryType") == "traceql" and panel.get("type") == "table"
    ]

    assert traceql_queries
    assert all("span.rag.turn_id" in query for query in traceql_queries)
    assert all("span.rag.trace_id" in query for query in traceql_queries)
