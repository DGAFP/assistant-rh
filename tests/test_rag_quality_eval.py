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
    config_fingerprint,
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


def test_retrieval_shortfall_caps_score_but_partial_can_pass() -> None:
    none_hit = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.0, "hit_rate": 0.0})
    assert {"reason": "no_expected_source_retrieved", "max_score": 0.6} in none_hit["calibration_caps"]
    assert none_hit["score"] == 0.6
    assert none_hit["pass"] is False

    partial = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.5, "hit_rate": 1.0})
    assert any(c["reason"] == "missing_expected_source" and c.get("soft") for c in partial["calibration_caps"])
    assert partial["score"] == 0.85
    assert partial["pass"] is True


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
    smoke = argparse.Namespace(limit=1, skip_ragas=False, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a")
    full = argparse.Namespace(limit=None, skip_ragas=False, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a")
    no_judge = argparse.Namespace(limit=1, skip_ragas=False, ragas_model="ragas-a", skip_judge=True, judge_model="judge-a")
    no_ragas = argparse.Namespace(limit=1, skip_ragas=True, ragas_model="ragas-a", skip_judge=False, judge_model="judge-a")

    smoke_scope = build_eval_scope(smoke, questions[:1])

    assert smoke_scope == {
        "limit": 1,
        "question_count": 1,
        "question_ids": [1],
        "ragas_enabled": True,
        "ragas_model": "ragas-a",
        "judge_enabled": True,
        "judge_model": "judge-a",
    }
    assert build_eval_scope(full, questions) != smoke_scope
    assert build_eval_scope(no_judge, questions[:1]) != smoke_scope
    assert build_eval_scope(no_ragas, questions[:1]) != smoke_scope


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
