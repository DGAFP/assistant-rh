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
    assert variables["postgres_datasource"]["type"] == "datasource"
    assert variables["postgres_datasource"]["query"] == "postgres"
    assert variables["env"]["includeAll"] is True
    assert variables["env"]["allValue"] == ".*"
    for name in ("turn_id", "trace_id", "stage", "source_table", "status"):
        assert name in variables


def test_trace_dashboard_uses_portable_datasource_uids() -> None:
    datasources = [panel["datasource"] for panel in _dashboard()["panels"] if "datasource" in panel]

    assert datasources
    assert {datasource["uid"] for datasource in datasources} == {"$trace_datasource", "$postgres_datasource"}
    assert all(not datasource["uid"].startswith("${") for datasource in datasources)


def test_trace_dashboard_contains_required_panels() -> None:
    titles = {panel["title"] for panel in _dashboard()["panels"]}

    assert "Recent RAG traces" in titles
    assert "Pipeline stage timeline" in titles
    assert "Chunks by stage (bounded previews)" in titles
    assert "Sources interrogated and retained" in titles
    assert "Errors, fallbacks, and provider status" in titles
    assert "Admin drilldown" in titles


def test_chunk_panel_reads_bounded_previews_from_trace_events() -> None:
    dashboard = _dashboard()
    chunk_panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Chunks by stage (bounded previews)")
    sql = chunk_panel["targets"][0]["rawSql"]

    assert "rag_trace_events" in sql
    assert "chunk->>'preview' AS preview" in sql
    assert "v3_full_prompt" not in sql
    assert "full_prompt" not in sql


def test_traceql_panels_filter_by_turn_and_trace() -> None:
    dashboard = _dashboard()
    traceql_queries = [
        target["query"] for panel in dashboard["panels"] for target in panel.get("targets", []) if target.get("queryType") == "traceql"
    ]

    assert traceql_queries
    assert all("span.rag.turn_id" in query for query in traceql_queries)
    assert all("span.rag.trace_id" in query for query in traceql_queries)
