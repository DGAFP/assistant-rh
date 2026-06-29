from __future__ import annotations

import argparse
import json

import pytest
from assistant_rh_rag_pipeline.config import get_default_config

from src.goldset.eval import (
    EvalItem,
    aggregate_items,
    calibrate_judge_result,
    config_fingerprint,
    deterministic_metrics,
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


def test_calibrate_caps_when_declared_source_not_retrieved() -> None:
    calibrated = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.0, "hit_rate": 0.0})

    assert {"reason": "no_expected_source_retrieved", "max_score": 0.6} in calibrated["calibration_caps"]
    assert calibrated["score"] == 0.6
    assert calibrated["pass"] is False


def test_calibrate_caps_on_partial_doc_recall() -> None:
    calibrated = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 0.5, "hit_rate": 1.0})

    assert {"reason": "missing_expected_source", "max_score": 0.85} in calibrated["calibration_caps"]
    assert calibrated["score"] == 0.85


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


def test_calibrate_passes_clean_answer_with_perfect_retrieval() -> None:
    calibrated = calibrate_judge_result(_perfect_parsed(), {"doc_recall": 1.0, "hit_rate": 1.0})

    assert calibrated["calibration_caps"] == []
    assert calibrated["score"] == 1.0
    assert calibrated["pass"] is True


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


def test_cli_argparse_namespace_smoke() -> None:
    args = argparse.Namespace(goldset_name="iteration2_V1", tag=["iteration2"], skip_ragas=True, skip_judge=True)
    assert args.goldset_name == "iteration2_V1"
