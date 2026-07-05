from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from assistant_rh_rag_pipeline.config import get_default_config

from src.goldset.eval import (
    EvalItem,
    GoldsetQuestion,
    aggregate_items,
    artifact_paths,
    build_eval_scope,
    calibrate_judge_result,
    compare_with_baseline,
    config_fingerprint,
    derive_completion_status,
    deterministic_metrics,
    load_goldset_questions,
    parse_text_list,
    write_artifacts,
)


def test_parse_text_list_accepts_json_and_delimiters() -> None:
    assert parse_text_list('["doc-a", "doc-b"]') == ["doc-a", "doc-b"]
    assert parse_text_list("doc-a; doc-b, doc-c") == ["doc-a", "doc-b", "doc-c"]
    assert parse_text_list(["doc-a", "", " doc-b "]) == ["doc-a", "doc-b"]


def test_deterministic_metrics_scores_source_overlap() -> None:
    metrics = deterministic_metrics(["doc-a", "doc-c"], ["doc-b", "doc-a", "doc-d"])

    assert metrics["gold_count"] == 2
    assert metrics["retrieved_doc_count"] == 3
    assert metrics["hit_count"] == 1
    assert metrics["doc_recall"] == 0.5
    assert metrics["doc_precision"] == 1 / 3
    assert metrics["hit_rate"] == 1.0
    assert metrics["mrr"] == 0.5
    assert metrics["missing_gold_sources"] == ["doc-c"]


def test_config_fingerprint_is_stable_and_changes_with_config() -> None:
    config_a = get_default_config()
    config_b = get_default_config()
    assert config_fingerprint(config_a) == config_fingerprint(config_b)

    config_b.retrieval.initial_top_k = config_a.retrieval.initial_top_k + 1
    assert config_fingerprint(config_a) != config_fingerprint(config_b)


def test_aggregate_items_averages_available_metrics() -> None:
    items = [
        EvalItem(
            question_id=1,
            question="q1",
            gold_answer="a",
            gold_sources=["doc-a"],
            deterministic_metrics={"doc_recall": 1.0, "doc_precision": 0.5, "hit_rate": 1.0, "mrr": 1.0},
            judge_result={
                "score": 0.8,
                "raw_model_score": 0.9,
                "status": "completed",
                "pass": True,
                "dimensions": {"legal_correctness": 0.8, "completeness": 0.8, "gold_answer_alignment": 0.8, "source_support": 0.8},
            },
            ragas_metrics={"faithfulness": 0.7, "context_precision": 0.6, "context_recall": 0.5},
        ),
        EvalItem(
            question_id=2,
            question="q2",
            gold_answer="a",
            gold_sources=["doc-b"],
            deterministic_metrics={"doc_recall": 0.0, "doc_precision": 0.0, "hit_rate": 0.0, "mrr": 0.0},
            judge_result={
                "score": 0.4,
                "raw_model_score": 0.5,
                "status": "completed",
                "pass": False,
                "dimensions": {"legal_correctness": 0.4, "completeness": 0.4, "gold_answer_alignment": 0.4, "source_support": 0.4},
            },
            ragas_metrics={"faithfulness": 0.3, "context_precision": 0.2, "context_recall": 0.1},
        ),
    ]

    aggregate = aggregate_items(items)

    assert aggregate["total"] == 2
    assert aggregate["completed"] == 2
    assert aggregate["doc_recall_avg"] == 0.5
    assert aggregate["judge_score_avg"] == pytest.approx(0.6)
    assert aggregate["judge_raw_score_avg"] == pytest.approx(0.7)
    assert aggregate["judge_pass_rate"] == 0.5
    assert aggregate["judge_legal_correctness_avg"] == pytest.approx(0.6)
    assert aggregate["ragas_faithfulness_avg"] == 0.5


def test_calibrate_judge_result_applies_contradiction_and_missing_source_caps() -> None:
    parsed = {
        "score": 1.0,
        "pass": True,
        "failure_category": "none",
        "material_contradiction": True,
        "dimensions": {
            "legal_correctness": 1.0,
            "completeness": 1.0,
            "gold_answer_alignment": 1.0,
            "source_support": 1.0,
        },
    }
    calibrated = calibrate_judge_result(parsed, {"doc_recall": 0.5, "hit_rate": 1.0})

    assert calibrated["raw_model_score"] == 1.0
    assert calibrated["score"] == 0.6
    assert calibrated["pass"] is False
    assert calibrated["failure_category"] == "quality_gate_failed"
    assert {"reason": "material_contradiction_with_gold_answer", "max_score": 0.6} in calibrated["calibration_caps"]


def _perfect_parsed() -> dict:
    return {
        "score": 1.0,
        "pass": True,
        "failure_category": "none",
        "material_contradiction": False,
        "dimensions": {
            "legal_correctness": 1.0,
            "completeness": 1.0,
            "gold_answer_alignment": 1.0,
            "source_support": 1.0,
        },
    }


def test_deterministic_metrics_matching_is_normalized() -> None:
    # Matching is dot/space/case insensitive, so a gold article code "L. 332-22"
    # hits a resolved/retrieved "L332-22".
    metrics = deterministic_metrics(["L. 332-22", "F8"], ["LEGIARTI000044426654", "L332-22", "f8"])

    assert metrics["hit_count"] == 2
    assert metrics["doc_recall"] == 1.0
    assert metrics["missing_gold_sources"] == []


def test_any_goldset_requires_at_least_one_tag() -> None:
    with pytest.raises(ValueError, match="--tag"):
        load_goldset_questions("postgresql://unused", goldset_name="baseline_v1", tags=[], any_goldset=True)


def test_any_goldset_runner_guard_runs_before_db_resolution(monkeypatch) -> None:
    import src.goldset.eval as eval_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("resolve_dsn should not be called before validating --any-goldset")

    monkeypatch.setattr(eval_module, "resolve_dsn", fail_if_called)

    with pytest.raises(ValueError, match="--tag"):
        eval_module.run_eval(argparse.Namespace(any_goldset=True, tag=[]))


def test_resolve_gold_doc_ids_maps_codes_names_and_ranges() -> None:
    from src.goldset.eval import resolve_gold_doc_ids

    maps = {
        "doc_short": {"MSO_TEMPS_DE_TRAVAIL_ABC": "uuid-mso"},
        "matte_short": {"A1": {"uuid-a1"}},
        "article": {"L631-3": {"LEGIA-3"}, "L631-4": {"LEGIA-4"}},
    }
    resolved = resolve_gold_doc_ids(["F8", "A1", "MSO_temps_de_travail_abc", "CGFP, L. 631-3 à L. 631-4"], maps)

    assert "F8" in resolved  # unresolved raw token kept (matches Service-Public doc_id directly)
    assert "A1" not in resolved  # resolved labels are alternatives, not extra required sources
    assert "uuid-a1" in resolved
    assert "uuid-mso" in resolved
    assert {"LEGIA-3", "LEGIA-4"} <= set(resolved)  # range expanded and resolved
    assert "CGFP, L. 631-3 à L. 631-4" not in resolved


def test_resolved_gold_doc_ids_do_not_penalize_raw_label_misses() -> None:
    from src.goldset.eval import resolve_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {"L631-3": {"LEGIA-3"}, "L631-4": {"LEGIA-4"}}}
    resolved = resolve_gold_doc_ids(["CGFP, L. 631-3 à L. 631-4"], maps)
    metrics = deterministic_metrics(resolved, ["LEGIA-3", "LEGIA-4"])

    assert metrics["doc_recall"] == 1.0
    assert metrics["missing_gold_sources"] == []


def test_deterministic_metrics_empty_gold_sources_yields_none_recall() -> None:
    metrics = deterministic_metrics([], ["doc-a", "doc-b"])

    assert metrics["doc_recall"] is None
    assert metrics["hit_rate"] == 0.0


def test_calibrate_passes_when_no_expected_sources_declared() -> None:
    # A question with no gold_sources produces doc_recall=None / hit_rate=0.0;
    # it must not be capped or failed for "retrieving none of the expected".
    calibrated = calibrate_judge_result(_perfect_parsed(), {"doc_recall": None, "hit_rate": 0.0})

    assert calibrated["calibration_caps"] == []
    assert calibrated["score"] == 1.0
    assert calibrated["pass"] is True


def test_retrieval_shortfall_caps_score_but_never_blocks_pass() -> None:
    # Arbitrage du 06/07/2026 (audit run 52): les métriques retrieval sont un
    # diagnostic — hit_rate=0 plafonne le score stocké (visibilité) mais ne
    # bloque plus le pass d'une réponse par ailleurs parfaite (le cap dur
    # rendait le pass impossible pour 29/100 questions, artefacts d'ids
    # compris, avec des rationales 100 % positifs à score 0.6).
    none_hit = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.0, "hit_rate": 0.0})
    assert any(c["reason"] == "no_expected_source_retrieved" and c.get("soft") for c in none_hit["calibration_caps"])
    assert none_hit["score"] == 0.6  # score stocké plafonné pour la visibilité
    assert none_hit["pass"] is True  # le pass s'évalue AVANT caps soft

    partial = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.5, "hit_rate": 1.0})
    assert any(c["reason"] == "missing_expected_source" and c.get("soft") for c in partial["calibration_caps"])
    assert partial["score"] == 0.85
    assert partial["pass"] is True


def test_retrieval_soft_cap_does_not_rescue_a_bad_answer() -> None:
    # Le découplage ne crée pas de faux pass: une réponse faible échoue
    # toujours sur ses dimensions, avec ou sans trou de retrieval.
    parsed = _perfect_parsed()
    parsed["dimensions"]["legal_correctness"] = 0.5
    calibrated = calibrate_judge_result(parsed, {"doc_recall": 0.0, "hit_rate": 0.0})
    assert calibrated["pass"] is False


@pytest.mark.parametrize(
    ("dimension", "value", "reason", "max_score"),
    [
        ("gold_answer_alignment", 0.7, "weak_gold_answer_alignment", 0.75),
        ("legal_correctness", 0.7, "legal_correctness_below_gate", 0.75),
        ("completeness", 0.6, "incomplete_answer", 0.8),
        ("source_support", 0.6, "weak_source_support", 0.75),
    ],
)
def test_calibrate_applies_each_dimension_cap(dimension, value, reason, max_score) -> None:
    parsed = _perfect_parsed()
    parsed["dimensions"][dimension] = value
    calibrated = calibrate_judge_result(parsed, {"doc_recall": 1.0, "hit_rate": 1.0})

    assert {"reason": reason, "max_score": max_score} in calibrated["calibration_caps"]
    assert calibrated["score"] <= max_score
    assert calibrated["pass"] is False


def test_calibrate_blocks_material_contradiction_regardless_of_score() -> None:
    # The judge must fail an answer that contradicts the gold answer on a material
    # point, even with otherwise strong dimensions (the dangerous false-PASS case).
    parsed = _perfect_parsed()
    parsed["material_contradiction"] = True
    calibrated = calibrate_judge_result(parsed, {})

    assert calibrated["pass"] is False
    assert calibrated["material_contradiction"] is True


def test_calibrate_rubric_is_injectable() -> None:
    from src.goldset.eval import DEFAULT_JUDGE_RUBRIC

    # An answer that fails the default completeness gate (0.75) ...
    parsed = _perfect_parsed()
    parsed["dimensions"]["completeness"] = 0.72
    assert calibrate_judge_result(dict(parsed), {})["pass"] is False

    # ... passes under a rubric with a lower completeness floor.
    from dataclasses import replace

    relaxed = replace(
        DEFAULT_JUDGE_RUBRIC,
        dimension_score_caps=tuple((d, r, (0.6 if d == "completeness" else f), m) for d, r, f, m in DEFAULT_JUDGE_RUBRIC.dimension_score_caps),
        pass_dimension_floors={**DEFAULT_JUDGE_RUBRIC.pass_dimension_floors, "completeness": 0.7},
    )
    assert calibrate_judge_result(dict(parsed), {}, relaxed)["pass"] is True


def test_calibrate_passes_clean_answer_with_perfect_retrieval() -> None:
    calibrated = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 1.0, "hit_rate": 1.0})

    assert calibrated["calibration_caps"] == []
    assert calibrated["score"] == 1.0
    assert calibrated["pass"] is True


def test_artifact_paths_match_written_files(tmp_path) -> None:
    # The DB-recorded paths must equal what write_artifacts actually writes.
    expected_json, expected_csv = artifact_paths(tmp_path, "run")
    json_path, csv_path = write_artifacts(tmp_path, "run", [], {"status": "completed"})

    assert (json_path, csv_path) == (expected_json, expected_csv)
    assert json_path == Path(tmp_path) / "run.json"
    assert json_path.exists() and csv_path.exists()


def test_write_artifacts_outputs_json_and_csv(tmp_path) -> None:
    item = EvalItem(
        question_id=1,
        question="Question ?",
        gold_answer="Réponse",
        gold_sources=["doc-a"],
        deterministic_metrics={"doc_recall": 1.0, "doc_precision": 1.0, "hit_rate": 1.0, "mrr": 1.0},
        judge_result={"score": 1.0, "pass": True, "failure_category": "none"},
        ragas_metrics={"faithfulness": 1.0, "context_precision": 1.0, "context_recall": 1.0},
        metadata={"theme": "Test"},
    )

    json_path, csv_path = write_artifacts(tmp_path, "run", [item], {"status": "completed"})

    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["status"] == "completed"
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "question_id,question,theme" in csv_text
    assert "Question ?" in csv_text


def test_build_eval_scope_separates_smoke_full_and_judge_modes() -> None:
    questions = [
        GoldsetQuestion(id=1, question="q1", gold_answer="a1", gold_sources=["doc-a"]),
        GoldsetQuestion(id=2, question="q2", gold_answer="a2", gold_sources=["doc-b"]),
    ]
    smoke = argparse.Namespace(limit=1, skip_ragas=False, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a", ministry_scope="all")
    full = argparse.Namespace(limit=None, skip_ragas=False, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a", ministry_scope="all")
    no_judge = argparse.Namespace(limit=1, skip_ragas=False, ragas_model="ragas-a", skip_judge=True, judge_model="judge-a", ministry_scope="all")
    no_ragas = argparse.Namespace(limit=1, skip_ragas=True, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a", ministry_scope="all")
    unscoped = argparse.Namespace(limit=1, skip_ragas=False, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a", ministry_scope="none")

    smoke_scope = build_eval_scope(smoke, questions[:1])

    assert smoke_scope == {
        "limit": 1,
        "question_count": 1,
        "question_ids": [1],
        "ragas_enabled": True,
        "ragas_model": "ragas-a",
        "judge_enabled": True,
        "judge_model": "judge-a",
        "ministry_scope": "all",
    }
    assert build_eval_scope(full, questions) != smoke_scope
    assert build_eval_scope(no_judge, questions[:1]) != smoke_scope
    assert build_eval_scope(no_ragas, questions[:1]) != smoke_scope
    # Un run scopé « all ministries » n'est pas comparable à un run historique.
    assert build_eval_scope(unscoped, questions[:1]) != smoke_scope


def test_baseline_comparison_passes_within_allowed_drop() -> None:
    eval_scope = {"question_ids": [1, 2], "judge_enabled": True, "ragas_enabled": False}
    baseline = {
        "id": 10,
        "status": "completed",
        "goldset_name": "iteration2_V1",
        "tag_filter": ["iteration2"],
        "run_label": "baseline",
        "git_sha": "abc",
        "config_fingerprint": "base",
        "aggregate": {"judge_pass_rate": 0.8, "doc_recall_avg": 0.7},
        "metadata": {"eval_scope": eval_scope},
    }

    comparison = compare_with_baseline(
        candidate_aggregate={"judge_pass_rate": 0.76, "doc_recall_avg": 0.66},
        baseline_run=baseline,
        goldset_name="iteration2_V1",
        tags=["iteration2"],
        eval_scope=eval_scope,
        max_judge_pass_rate_drop=0.05,
        max_doc_recall_drop=0.05,
    )

    assert comparison["passed"] is True
    assert comparison["metrics"]["judge_pass_rate"]["delta"] == pytest.approx(-0.04)


def test_baseline_comparison_fails_on_metric_regression() -> None:
    eval_scope = {"question_ids": [1], "judge_enabled": True, "ragas_enabled": False}
    baseline = {
        "id": 10,
        "status": "completed",
        "goldset_name": "iteration2_V1",
        "tag_filter": ["iteration2"],
        "aggregate": {"judge_pass_rate": 0.9, "doc_recall_avg": 0.8},
        "metadata": {"eval_scope": eval_scope},
    }

    comparison = compare_with_baseline(
        candidate_aggregate={"judge_pass_rate": 0.7, "doc_recall_avg": 0.79},
        baseline_run=baseline,
        goldset_name="iteration2_V1",
        tags=["iteration2"],
        eval_scope=eval_scope,
        max_judge_pass_rate_drop=0.05,
        max_doc_recall_drop=0.05,
    )

    assert comparison["passed"] is False
    assert comparison["status"] == "failed"
    assert "judge_pass_rate" in comparison["failures"][0]


def test_baseline_comparison_requires_comparable_scope() -> None:
    baseline = {
        "id": 10,
        "status": "completed",
        "goldset_name": "iteration2_V1",
        "tag_filter": ["iteration2"],
        "aggregate": {"judge_pass_rate": 0.9, "doc_recall_avg": 0.8},
        "metadata": {"eval_scope": {"question_ids": [1], "judge_enabled": True}},
    }

    comparison = compare_with_baseline(
        candidate_aggregate={"judge_pass_rate": 0.9, "doc_recall_avg": 0.8},
        baseline_run=baseline,
        goldset_name="iteration2_V1",
        tags=["iteration2"],
        eval_scope={"question_ids": [2], "judge_enabled": True},
        max_judge_pass_rate_drop=0.05,
        max_doc_recall_drop=0.05,
    )

    assert comparison["passed"] is False
    assert comparison["status"] == "not_comparable"
    assert comparison["comparability_failures"] == ["baseline eval_scope does not match candidate"]


def test_baseline_comparison_reports_missing_baseline() -> None:
    comparison = compare_with_baseline(
        candidate_aggregate={"judge_pass_rate": 0.9, "doc_recall_avg": 0.8},
        baseline_run=None,
        goldset_name="iteration2_V1",
        tags=["iteration2"],
        eval_scope={"question_ids": [1]},
        max_judge_pass_rate_drop=0.05,
        max_doc_recall_drop=0.05,
    )

    assert comparison["passed"] is False
    assert comparison["status"] == "missing_baseline"


def test_backfill_ragas_status_summary() -> None:
    from scripts.backfill_ragas import summarize_ragas_status

    assert summarize_ragas_status([{"answer": "a", "ragas_metrics": {"status": "completed"}}]) == (
        "completed",
        {"completed": 1, "failed": 0, "skipped": 0, "pending": 0},
    )
    assert summarize_ragas_status([{"answer": "a", "ragas_metrics": {"status": "failed"}}])[0] == "failed"
    assert (
        summarize_ragas_status(
            [
                {"answer": "a", "ragas_metrics": {"status": "completed"}},
                {"answer": "b", "ragas_metrics": {"status": "failed"}},
            ]
        )[0]
        == "partial"
    )
    assert summarize_ragas_status([{"answer": "", "ragas_metrics": {}}])[0] == "skipped"


def test_calibrate_load_labels_requires_question(tmp_path) -> None:
    from scripts.calibrate_judge import load_labels

    labels = tmp_path / "labels.csv"
    labels.write_text(
        "question,answer,gold_answer,verdict\n,answer,gold,PASS\nquestion,answer,gold,BLOCKS\n",
        encoding="utf-8",
    )

    usable = load_labels(labels)

    assert len(usable) == 1
    assert usable[0]["question"] == "question"


def test_resolve_goldset_doc_ids_dry_run_does_not_mutate(monkeypatch) -> None:
    import scripts.resolve_goldset_doc_ids as resolver

    executed: list[str] = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params=None):
            executed.append(sql)
            return self

        def fetchall(self):
            return [{"id": 1, "gold_sources": "F8"}]

    monkeypatch.setattr(resolver.psycopg, "connect", lambda *args, **kwargs: FakeConn())
    monkeypatch.setattr(resolver, "load_gold_id_maps", lambda dsn: {"doc_short": {}, "matte_short": {}, "article": {}})

    assert resolver.main(["--dsn", "postgresql://unused", "--dry-run"]) == 0
    assert not any("ALTER TABLE" in sql or "UPDATE public.goldset_questions_v2" in sql for sql in executed)


def test_cli_argparse_namespace_smoke() -> None:
    args = argparse.Namespace(goldset_name="iteration2_V1", tag=["iteration2"], skip_ragas=True, skip_judge=True)
    assert args.goldset_name == "iteration2_V1"


def _item(error: str = "", *, judge_status: str = "completed", ragas_status: str = "completed") -> EvalItem:
    return EvalItem(
        question_id=1,
        question="q",
        gold_answer="a",
        gold_sources=["doc-a"],
        judge_result={"status": judge_status},
        ragas_metrics={"status": ragas_status},
        error=error,
    )


def test_derive_status_all_items_errored_is_failure() -> None:
    # Regression: a run where every question failed to execute must be `failed`,
    # not a passing `completed_with_errors` — otherwise the eval exits 0 and CI
    # stays green while nothing actually ran.
    status, error = derive_completion_status(
        [_item(error="pipeline boom"), _item(error="pipeline boom")],
        judge_enabled=True,
        ragas_enabled=True,
    )
    assert status == "failed"
    assert "all 2 questions failed to execute" in error


def test_derive_status_partial_errors_is_completed_with_errors() -> None:
    status, error = derive_completion_status(
        [_item(), _item(error="one failed")],
        judge_enabled=True,
        ragas_enabled=True,
    )
    assert status == "completed_with_errors"
    assert error == ""


def test_derive_status_all_clean_is_completed() -> None:
    status, error = derive_completion_status([_item(), _item()], judge_enabled=True, ragas_enabled=True)
    assert status == "completed"
    assert error == ""


def test_derive_status_judge_failed_on_every_executed_item_is_failure() -> None:
    # Judge requested but errored on all executed items => config failure.
    status, error = derive_completion_status(
        [_item(judge_status="failed"), _item(judge_status="failed")],
        judge_enabled=True,
        ragas_enabled=False,
    )
    assert status == "failed"
    assert "judge requested but failed" in error


def test_derive_status_ignores_disabled_subtask() -> None:
    # Judge disabled: a "failed" judge status must not be escalated.
    status, error = derive_completion_status(
        [_item(judge_status="failed"), _item(judge_status="failed")],
        judge_enabled=False,
        ragas_enabled=True,
    )
    assert status == "completed"
    assert error == ""


def test_resolve_gold_doc_ids_keeps_multi_segment_codes() -> None:
    # Regression: a 3-segment code "L. 332-22-1" must resolve to its own article,
    # not collapse to the parent "L332-22" (which is a different source).
    from src.goldset.eval import resolve_gold_doc_ids

    maps = {
        "doc_short": {},
        "matte_short": {},
        "article": {"L332-22": {"PARENT"}, "L332-22-1": {"CHILD"}},
    }
    resolved = resolve_gold_doc_ids(["Article L. 332-22-1"], maps)

    assert "CHILD" in resolved
    assert "PARENT" not in resolved


def test_resolve_question_scope_per_question_routes_ministries() -> None:
    from src.goldset.eval import resolve_question_scope

    mso_question = GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[], source="MSO")
    manual_question = GoldsetQuestion(id=2, question="q", gold_answer="a", gold_sources=[], source="manual")

    mso_scope = resolve_question_scope(mso_question, "per-question")
    assert mso_scope.selected_ministry == "mso"
    assert "mso" in mso_scope.table_keys
    # Pas de contamination inter-ministères: matte absent du scope MSO.
    assert "matte" not in mso_scope.table_keys

    manual_scope = resolve_question_scope(manual_question, "per-question")
    assert manual_scope.selected_ministry == "eval_all_ministries"

    assert resolve_question_scope(mso_question, "none") is None
    assert resolve_question_scope(mso_question, "all").selected_ministry == "eval_all_ministries"
