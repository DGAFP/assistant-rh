from __future__ import annotations

import json
from pathlib import Path

import pytest
from assistant_rh_data_engineering.jobs.rag_eval_metrics import EVAL_COLUMNS, MAX_EVAL_RUNS, PREFIX, eval_run_metrics
from assistant_rh_data_engineering.jobs.rag_health_exporter import RagHealthCollector, render_prometheus


def run(**updates):
    return {
        "id": 253,
        "goldset_name": "iteration2_V1",
        "status": "completed",
        "judge_model": "judge",
        "created_epoch": 1000,
        "duration_seconds": 12,
        "eval_scope": {"limit": 5, "question_ids": [1, 2, 3, 4, 6]},
        "aggregate": {"total": 5, "failed": 0, "judge_pass_rate": 0.6, "doc_recall_avg": 0.73},
        **updates,
    }


def values(row, suffix):
    return [(value, labels) for name, value, labels in eval_run_metrics(row) if name == PREFIX + suffix]


def test_real_run_shape_preserves_sample_size_scores_and_identity():
    assert values(run(), "run_score")[0][0] == 0.73
    assert any(v == 5 and labels["result"] == "total" for v, labels in values(run(), "run_questions"))
    assert values(run(), "run_info")[0][1]["limit"] == "5"
    assert values(run(), "run_info")[0][1]["run_id"] == "253"


@pytest.mark.parametrize("missing", [None, "bad", float("nan"), float("inf"), True])
def test_missing_invalid_or_non_finite_scores_are_not_zero(missing):
    assert values(run(aggregate={"judge_score_avg": missing}), "run_score") == []
    assert values(run(aggregate={"judge_score_avg": 0}), "run_score")[0][0] == 0


@pytest.mark.parametrize("comparison", [{}, {"status": "not_comparable", "comparable": False}, {"status": "missing_baseline"}])
def test_unproven_baselines_never_publish_a_delta(comparison):
    comparison = {**comparison, "baseline_run_id": 200, "metrics": {"judge_pass_rate": {"delta": -0.2}}}
    assert values(run(aggregate={"baseline_comparison": comparison}), "baseline_delta") == []


def test_recorded_comparable_delta_and_quality_failure_are_preserved():
    comparison = {"status": "failed", "comparable": True, "baseline_run_id": 200, "metrics": {"judge_pass_rate": {"delta": -0.2}}}
    row = run(status="failed_quality_gate", aggregate={"baseline_comparison": comparison})
    assert values(row, "baseline_delta")[0] == (
        -0.2,
        {"run_id": "253", "goldset": "iteration2_V1", "metric": "judge_pass_rate", "baseline_run_id": "200"},
    )
    assert values(row, "run_status")[0][0] == 3
    assert values(row, "baseline_status")[0][0] == 2


def test_stage_attempts_and_question_counts_stay_distinct():
    row = run(
        aggregate={
            "stage_metrics": {"initial": {"pool": {"n": 5, "doc_recall_avg": 0.2}}, "selector_retry": {"pool": {"n": 2, "doc_recall_avg": 0.8}}}
        }
    )
    assert [(value, labels["attempt"]) for value, labels in values(row, "stage_score")] == [(0.2, "initial"), (0.8, "selector_retry")]
    assert [(value, labels["attempt"]) for value, labels in values(row, "stage_questions")] == [(5, "initial"), (2, "selector_retry")]


def test_sensitive_payloads_and_arbitrary_dimensions_do_not_become_metrics():
    row = run(question="private question", answer="private answer", aggregate={"secret": 99, "question": "private question"})
    serialized = json.dumps(eval_run_metrics(row))
    assert "private" not in serialized
    assert "secret" not in serialized
    assert "question_ids" not in serialized
    assert values(run(duration_seconds=None, status="started"), "run_duration_seconds") == []


def test_optional_schema_can_be_absent_without_a_query():
    samples = RagHealthCollector(env_label="staging")._eval_metrics(object(), {})
    assert len(samples) == 1 and samples[0].value == 0


def test_collector_bounds_query_and_distinguishes_empty_database():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            assert "ORDER BY id DESC LIMIT %s" in sql
            assert params == (MAX_EVAL_RUNS,)
            assert "rag_quality_eval_items" not in sql

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

    samples = RagHealthCollector(env_label="prod")._eval_metrics(Connection(), {"rag_quality_eval_runs": set(EVAL_COLUMNS)})
    output = render_prometheus(samples)
    assert 'assistant_rh_rag_eval_schema_available{env="prod"} 1' in output
    assert 'assistant_rh_rag_eval_runs_exposed{env="prod"} 0' in output


def test_dashboard_uses_recorded_instant_values_and_exact_selected_run():
    path = Path(__file__).resolve().parents[1] / "config/grafana/rag-eval-dashboard.json"
    dashboard = json.loads(path.read_text())
    assert dashboard["uid"] == "ef5g8p2"
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["env"]["current"]["value"] == "staging"
    assert variables["run"]["multi"] is False
    for name in ("goldset", "run"):
        assert variables[name]["query"].startswith("query_result(")
        assert "label_values" not in variables[name]["query"]
    for panel in dashboard["panels"]:
        for query in panel.get("targets", []):
            assert query["instant"] is True
            assert panel["datasource"]["uid"] == "$datasource"
            if panel["id"] not in {1, 15}:
                assert 'run_id="$run"' in query["expr"]
    assert not any(p["type"] == "timeseries" for p in dashboard["panels"])
