import json
from pathlib import Path

import pytest
from assistant_rh_data_engineering.jobs.rag_eval_items import ITEM_COLUMNS, MAX_EVAL_ITEMS_PER_RUN, eval_item_metrics
from assistant_rh_data_engineering.jobs.rag_eval_metrics import PREFIX
from assistant_rh_data_engineering.jobs.rag_health_exporter import MetricsState, RagHealthCollector, metric


def item(item_id=1, *, stages=None, **updates):
    return {
        "id": item_id,
        "question_id": 10,
        "question": "Question de test",
        "gold_sources": ["F8"],
        "deterministic_metrics": {"gold_count": 1, "stages": stages or {}},
        **updates,
    }


def samples(items, suffix, **labels):
    return [
        (v, ls)
        for name, v, ls in eval_item_metrics({"id": 240, "goldset_name": "baseline_v1"}, items)
        if name == PREFIX + suffix and all(ls.get(k) == val for k, val in labels.items())
    ]


def value(items, suffix, **labels):
    rows = samples(items, suffix, **labels)
    assert len(rows) == 1
    return rows[0][0]


def stage(hit=1, recall=1):
    return {"hit_rate": hit, "doc_recall": recall, "doc_count": 3}


def test_global_union_hit_does_not_hide_a_loss_in_context():
    i = item(stages={"initial": {"pool": stage(), "sections_top20": stage(), "selector_kept": stage(0, 0), "context_builder_output": stage(0, 0)}})
    i["deterministic_metrics"].update(hit_rate=1, doc_recall=1)
    assert value([i], "item_stage_state", attempt="initial", stage="context_builder_output") == 0
    assert value([i], "funnel_score", attempt="initial", stage="context_builder_output", metric="doc_recall") == 0
    assert value([i], "transition_questions", attempt="initial", transition="sections_top20_to_selector_kept", measure="lost") == 1


def test_denominators_are_metric_specific_and_missing_is_not_a_miss():
    items = [
        item(1, stages={"initial": {"pool": stage(1, 0.5)}}),
        item(2, stages={"initial": {"pool": stage(0, None)}}),
        item(3),
        item(4, deterministic_metrics={"gold_count": 0, "stages": {"initial": {"pool": stage(0, None)}}}),
    ]
    labels = {"attempt": "initial", "stage": "pool"}
    assert value(items, "funnel_questions", **labels, measure="hit_measured") == 2
    assert value(items, "funnel_questions", **labels, measure="recall_measured") == 1
    assert value(items, "funnel_questions", **labels, measure="trace_missing") == 1
    assert value(items, "funnel_questions", **labels, measure="no_gold") == 1
    assert value(items, "funnel_score", **labels, metric="hit_rate") == 0.5
    assert value(items, "funnel_score", **labels, metric="doc_recall") == 0.5


def test_paired_losses_exclude_unmeasured_endpoints_and_top12_is_not_a_transition():
    items = [
        item(1, stages={"initial": {"pool": stage(), "sections_top20": stage(0, 0)}}),
        item(2, stages={"initial": {"pool": stage()}}),
        item(3, stages={"initial": {"sections_top20": stage(0, 0)}}),
    ]
    labels = {"attempt": "initial", "transition": "pool_to_sections_top20"}
    assert value(items, "transition_questions", **labels, measure="paired") == 1
    assert value(items, "transition_questions", **labels, measure="lost") == 1
    assert "top12" not in json.dumps(samples(items, "transition_questions"))


def test_retry_and_initial_are_not_mixed_and_duplicate_question_ids_remain_distinct():
    items = [
        item(1, stages={"initial": {"pool": stage(0, 0)}, "selector_retry": {"pool": stage()}}),
        item(2, stages={"initial": {"pool": stage(0, 0)}}),
    ]
    assert len(samples(items, "item_info")) == 2
    assert value(items, "funnel_score", attempt="initial", stage="pool", metric="hit_rate") == 0
    assert value(items, "funnel_score", attempt="selector_retry", stage="pool", metric="hit_rate") == 1
    assert value(items, "funnel_questions", attempt="selector_retry", stage="pool", measure="hit_measured") == 1


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), -1, 2, "bad"])
def test_invalid_measurements_do_not_turn_into_valid_hits_or_recalls(bad):
    items = [item(stages={"initial": {"pool": stage(bad, bad)}})]
    assert value(items, "item_stage_state", attempt="initial", stage="pool") == -3
    assert samples(items, "funnel_score", attempt="initial", stage="pool") == []


def test_oversized_runs_have_no_partial_item_metrics_or_averages():
    rows = [item(i) for i in range(MAX_EVAL_ITEMS_PER_RUN + 1)]
    assert value(rows, "items_status") == 2
    assert value(rows, "items_exposed") == 0
    assert samples(rows, "item_info") == []
    assert samples(rows, "funnel_questions") == []


def test_question_excerpts_are_bounded_and_answers_and_judge_reasoning_are_not_exported():
    rows = [
        item(
            question="x" * 800,
            gold_sources=["s" * 800],
            answer="ANSWER_SECRET",
            metadata={"prompt": "PROMPT_SECRET"},
            judge_result={"status": "failed", "pass": True, "score": 1, "reason": "JUDGE_SECRET"},
        )
    ]
    info = samples(rows, "item_info")[0][1]
    assert len(info["question"]) == len(info["gold_sources"]) == 500
    text = json.dumps(eval_item_metrics({"id": 240}, rows))
    assert "SECRET" not in text
    assert value(rows, "item_judge_pass") == -1
    assert samples(rows, "item_judge_score") == []


def test_optional_item_schema_does_not_query_when_absent():
    output = RagHealthCollector(env_label="staging")._eval_item_metrics(object(), {}, [{"id": 240}])
    assert len(output) == 1 and output[0].value == 0


def test_item_query_is_bounded_per_run_and_selects_only_diagnostic_fields():
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            assert "CROSS JOIN LATERAL" in sql and "WHERE run_id = r.run_id ORDER BY id LIMIT %s" in sql
            assert params == ([240], MAX_EVAL_ITEMS_PER_RUN + 1)
            assert all(s not in sql for s in ("answer", "contexts", "metadata", "SELECT *"))

        def fetchall(self):
            return [(1, 240, 10, "Question", ["F8"], {"gold_count": 1, "stages": {}}, {})]

    class Connection:
        def cursor(self):
            return Cursor()

    result = RagHealthCollector(env_label="staging")._eval_item_metrics(
        Connection(), {"rag_quality_eval_items": set(ITEM_COLUMNS)}, [{"id": 240, "goldset_name": "baseline_v1"}]
    )
    assert any(s.name == PREFIX + "item_info" and s.labels["question_id"] == "10" for s in result)


def test_dashboard_context_kpis_use_stage_metrics_and_question_tables_keep_missing_values():
    d = json.loads((Path(__file__).resolve().parents[1] / "config/grafana/rag-eval-dashboard.json").read_text())
    panels = {p["id"]: p for p in d["panels"]}
    for panel_id in (6, 7):
        expr = panels[panel_id]["targets"][0]["expr"]
        assert 'stage="context_builder_output"' in expr and 'attempt="$attempt"' in expr
        assert "run_score" not in expr
    for panel_id in (23, 24):
        assert panels[panel_id]["transformations"][0]["options"] == {"byField": "item_id", "mode": "outerTabular"}
    assert "hit_measured" in json.dumps(panels[20]) and "recall_measured" in json.dumps(panels[20])


def test_separate_scrapes_are_disjoint_and_keep_the_combined_endpoint_compatible():
    state = MetricsState("staging")
    state.record_success([metric(PREFIX + "items_exposed", "staging", 98), metric("assistant_rh_rag_documents", "staging", 10)], 1)

    def numeric_lines(text):
        return {line for line in text.splitlines() if line and not line.startswith("#")}

    health, evals, all_metrics = (numeric_lines(state.render(group)) for group in ("health", "evals", "all"))
    assert health and evals and health.isdisjoint(evals)
    assert health | evals == all_metrics
    assert all(line.startswith(PREFIX) for line in evals)
    config = (Path(__file__).resolve().parents[1] / "config/grafana-alloy/rag-health.alloy.template").read_text()
    assert 'metrics_path    = "/metrics/health"' in config and 'scrape_interval = "60s"' in config
    assert 'metrics_path    = "/metrics/evals"' in config and 'scrape_interval = "120s"' in config
