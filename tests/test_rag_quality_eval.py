from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from assistant_rh_rag_pipeline.config import get_default_config

from src.goldset.eval import (
    EvalItem,
    GoldsetQuestion,
    aggregate_items,
    artifact_paths,
    borderline_question_ids,
    build_eval_scope,
    calibrate_judge_result,
    compare_with_baseline,
    config_fingerprint,
    derive_completion_status,
    deterministic_metrics,
    judge_pass_rate_excluding,
    load_goldset_questions,
    parse_text_list,
    retrieved_doc_ids,
    stage_retrieval_metrics,
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


def test_resolve_gold_doc_ids_maps_decree_article_references() -> None:
    from src.goldset.eval import resolve_gold_doc_ids

    maps = {
        "doc_short": {},
        "matte_short": {},
        "article": {},
        "legal_ref": {
            "DECREE:86-83:ARTICLE:10": {"LEGIA-10"},
            "DECREE:86-83:ARTICLE:11": {"LEGIA-11"},
            "DECREE:86-83:ARTICLE:12": {"LEGIA-12"},
            "DECREE:86-83:ARTICLE:15": {"LEGIA-15"},
        },
    }

    assert resolve_gold_doc_ids(["Decret 86-83, Article 15"], maps) == ["LEGIA-15"]
    assert resolve_gold_doc_ids(["Décret 86-83, Articles 10 à 12"], maps) == ["LEGIA-10", "LEGIA-11", "LEGIA-12"]


def test_resolve_gold_doc_ids_does_not_credit_stale_legacy_legifrance_text() -> None:
    from src.goldset.eval import resolve_gold_doc_ids

    maps = {
        "doc_short": {},
        "matte_short": {},
        "article": {"R331-2": {"LEGIARTI000051962495"}},
        "legal_ref": {},
    }

    assert resolve_gold_doc_ids(["Decret 86-83, Art.3"], maps) == ["Decret 86-83, Art.3"]


def test_deterministic_metrics_treats_document_aliases_as_equivalent() -> None:
    aliases = {
        "UUID-DOC": {"LEGIARTI000045662556"},
        "LEGIARTI000045662556": {"uuid-doc"},
    }

    metrics = deterministic_metrics(["uuid-doc"], ["LEGIARTI000045662556"], aliases=aliases)

    assert metrics["hit_rate"] == 1.0
    assert metrics["doc_recall"] == 1.0
    assert metrics["missing_gold_sources"] == []


def test_retrieved_doc_ids_collects_canonical_trace_alias_columns() -> None:
    class Result:
        metadata = {
            "chunks_raw": [
                {"cid": "LEGIARTI000045662556"},
                {"metadata": {"source_document_id": "doc-from-nested-metadata"}},
            ],
            "chunks_after_rerank": [{"short_id": "F1606"}],
            "context_items_ref": [{"document_id": "doc-from-context-ref"}],
        }

    contexts = [{"doc_id": "doc-from-context", "metadata": {"doc_short_id": "LEGIARTI000051268709"}}]

    ids = retrieved_doc_ids(Result(), contexts)  # type: ignore[arg-type]

    assert "LEGIARTI000045662556" in ids
    assert "doc-from-nested-metadata" in ids
    assert "F1606" in ids
    assert "doc-from-context-ref" in ids
    assert "LEGIARTI000051268709" in ids


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
    base = dict(
        skip_ragas=False,
        ragas_model="ragas-a",
        skip_judge=False,
        judge_model="judge-a",
        judge_provider="openrouter",
        judge_base_url="https://openrouter.ai/api/v1",
    )
    smoke = argparse.Namespace(limit=1, ministry_scope="all", **base)
    full = argparse.Namespace(limit=None, ministry_scope="all", **base)
    no_judge = argparse.Namespace(limit=1, ministry_scope="all", **{**base, "skip_judge": True})
    no_ragas = argparse.Namespace(limit=1, ministry_scope="all", **{**base, "skip_ragas": True})
    unscoped = argparse.Namespace(limit=1, ministry_scope="none", **base)
    # Endpoint différent -> eval_scope différent (revue #318: la comparabilité
    # doit distinguer deux runs sur des base URL distinctes).
    other_endpoint = argparse.Namespace(limit=1, ministry_scope="all", **{**base, "judge_base_url": "https://example.test/v1"})

    smoke_scope = build_eval_scope(smoke, questions[:1])

    assert smoke_scope == {
        "limit": 1,
        "question_count": 1,
        "question_ids": [1],
        "ragas_enabled": True,
        "ragas_model": "ragas-a",
        "judge_enabled": True,
        "judge_provider": "openrouter",
        "judge_base_url": "https://openrouter.ai/api/v1",
        "judge_model": "judge-a",
        "judge_votes": 1,
        "ministry_scope": "all",
    }
    assert build_eval_scope(full, questions) != smoke_scope
    assert build_eval_scope(no_judge, questions[:1]) != smoke_scope
    assert build_eval_scope(no_ragas, questions[:1]) != smoke_scope
    # Un run scopé « all ministries » n'est pas comparable à un run historique.
    assert build_eval_scope(unscoped, questions[:1]) != smoke_scope
    # Ni un run sur un autre endpoint de juge.
    assert build_eval_scope(other_endpoint, questions[:1]) != smoke_scope


def test_resolve_judge_endpoint_drives_key_and_base_url(monkeypatch) -> None:
    from src.goldset.eval import DEFAULT_SCALEWAY_JUDGE_MODEL, resolve_judge_endpoint, resolve_judge_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("SCALEWAY_API_KEY", "scw-key")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)

    # openrouter -> clé OpenRouter + base URL OpenRouter par défaut.
    provider, base_url, api_key = resolve_judge_endpoint("openrouter")
    assert (provider, api_key, base_url) == ("openrouter", "or-key", "https://openrouter.ai/api/v1")

    # scaleway -> clé Scaleway + base URL Scaleway (le provider pilote vraiment
    # la clé ET l'endpoint; #318: plus de scaleway inscrit avec la clé OpenRouter).
    provider, base_url, api_key = resolve_judge_endpoint("scaleway")
    assert (provider, api_key, base_url) == ("scaleway", "scw-key", "https://api.scaleway.ai/v1")

    # L'override explicite prime sur la var d'env et le défaut.
    _, base_url, _ = resolve_judge_endpoint("openrouter", "https://custom.test/v1")
    assert base_url == "https://custom.test/v1"

    # openai -> base URL par défaut VALIDE (revue #318: une URL vide levait
    # UnsupportedProtocol côté client OpenAI).
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    provider, base_url, api_key = resolve_judge_endpoint("openai")
    assert (provider, api_key, base_url) == ("openai", "oa-key", "https://api.openai.com/v1")

    # Le modèle suit lui aussi le provider: Scaleway ne doit jamais hériter du
    # défaut OpenRouter x-ai/grok-4.5.
    monkeypatch.delenv("OPENROUTER_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("SCALEWAY_JUDGE_MODEL", raising=False)
    assert resolve_judge_model("openrouter") == "x-ai/grok-4.5"
    assert resolve_judge_model("scaleway") == DEFAULT_SCALEWAY_JUDGE_MODEL
    assert resolve_judge_model("scaleway", "custom-scaleway-model") == "custom-scaleway-model"
    with pytest.raises(ValueError, match="OPENAI_JUDGE_MODEL"):
        resolve_judge_model("openai")


def test_judge_vote_count_is_bounded_and_shared_with_eval_scope() -> None:
    from src.goldset.eval import normalize_judge_votes

    assert normalize_judge_votes(1) == 1
    assert normalize_judge_votes("3") == 3
    for invalid in (-1, 0, 2, 4, 100_000):
        with pytest.raises(ValueError, match="one of: 1, 3"):
            normalize_judge_votes(invalid)

    args = argparse.Namespace(
        limit=1,
        skip_ragas=True,
        ragas_model="",
        skip_judge=False,
        judge_model="judge-a",
        judge_provider="openrouter",
        judge_base_url="https://openrouter.ai/api/v1",
        judge_votes=-1,
        ministry_scope="all",
    )
    with pytest.raises(ValueError, match="one of: 1, 3"):
        build_eval_scope(args, [GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[])])


def test_judge_answer_missing_key_message_names_provider() -> None:
    from src.goldset.eval import judge_answer

    result = judge_answer(
        question="q",
        gold_answer="a",
        answer="b",
        contexts=[],
        deterministic_metrics={},
        model="m",
        base_url="https://api.scaleway.ai/v1",
        api_key="",
        provider="scaleway",
    )
    # Le diagnostic nomme la var d'env du provider effectif, pas OpenRouter en dur.
    assert result["status"] == "skipped"
    assert "SCALEWAY_API_KEY" in result["reason"]
    assert "scaleway" in result["reason"]


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


@pytest.mark.parametrize(
    ("judge_enabled", "legacy_votes"),
    [
        (True, 1),
        (False, 0),
    ],
)
def test_baseline_comparison_backfills_legacy_judge_votes(judge_enabled: bool, legacy_votes: int) -> None:
    candidate_scope = {
        "question_ids": [1],
        "judge_enabled": judge_enabled,
        "judge_votes": legacy_votes,
        "ragas_enabled": False,
    }
    legacy_scope = {key: value for key, value in candidate_scope.items() if key != "judge_votes"}
    baseline = {
        "id": 10,
        "status": "completed",
        "goldset_name": "iteration2_V1",
        "tag_filter": ["iteration2"],
        "aggregate": {"judge_pass_rate": 0.9 if judge_enabled else None, "doc_recall_avg": 0.8},
        "metadata": {"eval_scope": legacy_scope},
    }

    comparison = compare_with_baseline(
        candidate_aggregate={"judge_pass_rate": 0.9 if judge_enabled else None, "doc_recall_avg": 0.8},
        baseline_run=baseline,
        goldset_name="iteration2_V1",
        tags=["iteration2"],
        eval_scope=candidate_scope,
        max_judge_pass_rate_drop=0.05,
        max_doc_recall_drop=0.05,
    )

    assert comparison["comparable"] is True


def test_eval_scope_variants_cover_combined_legacy_keys() -> None:
    from src.goldset.eval import _eval_scope_variants

    scope = {"judge_enabled": True, "judge_votes": 1, "ministry_scope": "none", "question_ids": [1]}
    variants = _eval_scope_variants(scope)

    assert scope in variants
    assert {"judge_enabled": True, "ministry_scope": "none", "question_ids": [1]} in variants
    assert {"judge_enabled": True, "judge_votes": 1, "question_ids": [1]} in variants
    assert {"judge_enabled": True, "question_ids": [1]} in variants


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


def test_calibrate_judge_uses_provider_specific_default_model(tmp_path, monkeypatch) -> None:
    from scripts import calibrate_judge
    from src.goldset.eval import DEFAULT_SCALEWAY_JUDGE_MODEL

    seen: dict[str, str] = {}

    def fake_judge_answer(**kwargs):
        seen["model"] = kwargs["model"]
        seen["provider"] = kwargs["provider"]
        return {"status": "completed", "dimensions": {}, "raw_model_score": 1.0}

    monkeypatch.setenv("JUDGE_PROVIDER", "scaleway")
    monkeypatch.setenv("SCALEWAY_API_KEY", "scw-key")
    monkeypatch.delenv("SCALEWAY_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(calibrate_judge, "judge_answer", fake_judge_answer)
    rows = calibrate_judge.capture_judge(
        [{"question": "q", "answer": "a", "gold_answer": "g", "verdict": "PASS"}],
        tmp_path / "cache.json",
    )

    assert len(rows) == 1
    assert seen == {"model": DEFAULT_SCALEWAY_JUDGE_MODEL, "provider": "scaleway"}


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

        def fetchone(self):
            # Probe information_schema: la colonne gold_doc_ids n'existe pas
            # encore (dry-run avant l'ALTER) — le script doit alors merger
            # depuis un existant vide.
            return None

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


def test_derive_status_official_protocol_rejects_partial_judge_results() -> None:
    status, error = derive_completion_status(
        [_item(judge_status="completed"), _item(judge_status="failed")],
        judge_enabled=True,
        ragas_enabled=False,
        judge_votes=3,
    )

    assert status == "failed"
    assert "completed 1/2 judgments" in error


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

    # Les questions manual ont été collectées auprès d'agents MATTE:
    # elles suivent le parcours MATTE (décision Paul 06/07/2026).
    manual_scope = resolve_question_scope(manual_question, "per-question")
    assert manual_scope.selected_ministry == "matte"
    assert "mso" not in manual_scope.table_keys

    # Sources non ministérielles (synthetic/Service-Public/DGAFP) -> parcours
    # MATTE aussi (matte + SP + Légifrance), pas scope complet (décision Paul
    # 06/07/2026): leur gold vit dans SP/Légifrance, présents dans tout scope
    # ministériel, et le scope complet leur infligeait le pool le plus bruité.
    for src in ("synthetic", "Service-Public", "DGAFP"):
        scope = resolve_question_scope(
            GoldsetQuestion(id=3, question="q", gold_answer="a", gold_sources=[], source=src),
            "per-question",
        )
        assert scope.selected_ministry == "matte", src
        assert "mso" not in scope.table_keys and "mi" not in scope.table_keys, src
        assert "service_public" in scope.table_keys, src

    assert resolve_question_scope(mso_question, "none") is None
    assert resolve_question_scope(mso_question, "all").selected_ministry == "eval_all_ministries"


class _FakeCursorResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Connexion factice: 1er execute = probe colonne, 2e = requête principale.

    ``probe_error`` simule une erreur transitoire sur le probe de colonne.
    """

    def __init__(self, *, has_column, main_rows, probe_error=None):
        self._has_column = has_column
        self._main_rows = main_rows
        self._probe_error = probe_error
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self._calls += 1
        if self._calls == 1:  # probe information_schema
            if self._probe_error is not None:
                raise self._probe_error
            return _FakeCursorResult([{"?column?": 1}] if self._has_column else [])
        return _FakeCursorResult(self._main_rows)


def test_load_goldset_uses_gold_doc_ids_when_column_present(monkeypatch) -> None:
    import src.goldset.eval as eval_module

    rows = [
        {
            "id": 7,
            "question": "q",
            "gold_answer": "a",
            "gold_sources": ["F1"],
            "theme": "",
            "tags": ["baseline_v1"],
            "goldset_name": "reference_v1",
            "source": "MATTE",
            "gold_doc_ids": ["F1", "uuid-resolved-1"],
        }
    ]
    monkeypatch.setattr(
        eval_module.psycopg,
        "connect",
        lambda *a, **k: _FakeConn(has_column=True, main_rows=rows),
    )
    qs = eval_module.load_goldset_questions("postgresql://x", goldset_name="reference_v1", tags=["baseline_v1"], any_goldset=True)
    assert qs[0].gold_doc_ids == ["F1", "uuid-resolved-1"]
    # retrieval_gold DOIT prendre les doc_ids résolus, pas les labels bruts.
    assert qs[0].retrieval_gold == ["F1", "uuid-resolved-1"]


def test_load_goldset_propagates_probe_error_not_silent_degrade(monkeypatch) -> None:
    # Régression run 67 (06/07/2026): un probe de colonne échouant en silence
    # (ancien _column_exists) faisait charger gold_doc_ids=NULL pour TOUT le run
    # -> matching sur labels bruts, hit_rate/doc_recall corrompus, juge biaisé.
    # Le probe partage désormais la connexion de la requête: l'erreur se propage.
    import psycopg

    import src.goldset.eval as eval_module

    monkeypatch.setattr(
        eval_module.psycopg,
        "connect",
        lambda *a, **k: _FakeConn(has_column=True, main_rows=[], probe_error=psycopg.OperationalError("transient")),
    )
    with pytest.raises(psycopg.OperationalError):
        eval_module.load_goldset_questions("postgresql://x", goldset_name="reference_v1", tags=["baseline_v1"], any_goldset=True)


def test_merge_gold_doc_ids_keeps_pre_resolved_and_unions_runtime() -> None:
    # Régression #276 (runs 67/68): resolve_gold_doc_ids ne mappe pas les
    # F-codes MATTE -> doc UUID; l'écrasement jetait l'UUID pré-résolu (pont)
    # -> hit_rate=0 alors que le gold est retrouvé. merge_gold_doc_ids garde
    # l'union: colonne curée ∪ résolution runtime.
    from src.goldset.eval import merge_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {"L332-4": {"LEGIA-4"}}, "legal_ref": {}, "aliases": {}}
    # gold_sources = un F-code MATTE (irrésoluble par les maps) + un article CGFP.
    pre_resolved = ["F1", "uuid-fiche-matte-1"]  # pont: F1 -> uuid
    merged = merge_gold_doc_ids(pre_resolved, ["F1", "CGFP L. 332-4"], maps)

    assert "uuid-fiche-matte-1" in merged  # l'UUID du pont n'est PAS jeté
    assert "LEGIA-4" in merged  # la résolution runtime (#276) est ajoutée


def test_merge_gold_doc_ids_empty_pre_resolved_falls_back_to_runtime() -> None:
    from src.goldset.eval import merge_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {"L332-4": {"LEGIA-4"}}, "legal_ref": {}, "aliases": {}}
    merged = merge_gold_doc_ids([], ["CGFP L. 332-4"], maps)
    assert merged == ["LEGIA-4"]


def test_merge_gold_doc_ids_drops_raw_label_that_sits_in_the_column() -> None:
    # Constat live (46/116 lignes staging): l'ANCIEN script écrivait le F-code
    # brut DANS la colonne, à côté du pont UUID (gold_doc_ids = ['F1', 'uuid']).
    # Ids résolus et label brut sont des ALTERNATIVES pour la même source:
    # garder 'F1' (jamais-matchable) rendait doc_recall < 1.0 structurel, cap
    # soft missing_expected_source et faux missing_gold_sources au juge. Le brut
    # co-résident d'un id résolu DANS la colonne est retiré -> ré-exécuter le
    # script nettoie la colonne en place.
    from src.goldset.eval import merge_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {}, "legal_ref": {}, "aliases": {}}
    assert merge_gold_doc_ids(["F1", "uuid-fiche-matte-1"], ["F1"], maps) == ["uuid-fiche-matte-1"]

    # Sans colonne curée, le label brut reste le seul candidat de matching
    # (alias/url-tail au moment des métriques) — il est conservé.
    assert merge_gold_doc_ids([], ["F1"], maps) == ["F1"]


def test_merge_gold_doc_ids_keeps_passthrough_of_uncovered_source() -> None:
    # Re-review follow-up PR #281: multi-source. La colonne ne ponte QUE la
    # source A (uuid). La source B est irrésoluble et n'est PAS dans la colonne
    # -> son label brut (passthrough runtime) est son SEUL ancrage et doit
    # survivre, même si A résout. Le retirer gonflerait doc_recall et masquerait
    # le retrieval gap de B. Le filtre ne touche donc QUE les bruts déjà présents
    # dans la colonne curée, pas les passthrough de sources non couvertes.
    from src.goldset.eval import merge_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {}, "legal_ref": {}, "aliases": {}}
    merged = merge_gold_doc_ids(["uuid-A"], ["A", "arrete-B-irresoluble"], maps)

    assert "uuid-A" in merged
    assert "arrete-B-irresoluble" in merged  # ancrage de la source B préservé


def test_merge_gold_doc_ids_keeps_raw_labels_when_nothing_resolves() -> None:
    # Aucune source ne résout et pas de pont: les labels bruts sont le seul
    # ancrage best-effort (alias/url-tail), on ne doit pas les jeter.
    from src.goldset.eval import merge_gold_doc_ids

    maps = {"doc_short": {}, "matte_short": {}, "article": {}, "legal_ref": {}, "aliases": {}}
    merged = merge_gold_doc_ids(["F3", "F4"], ["F3", "F4"], maps)

    assert set(merged) == {"F3", "F4"}


def test_usage_cost_eur_is_provider_aware() -> None:
    from src.goldset.eval import _usage_cost_eur

    # qwen3-235b: 0.75 € / 2.25 € par M tokens (in / out)
    assert _usage_cost_eur("scaleway", "qwen3-235b-a22b-instruct-2507", 1_000_000, 1_000_000) == 3.0
    # Même modèle chez un autre provider: aucune conversion EUR inventée.
    assert _usage_cost_eur("openrouter", "qwen3-235b-a22b-instruct-2507", 1_000_000, 1_000_000) is None
    assert _usage_cost_eur("scaleway", "modele-inconnu", 1000, 1000) is None


def test_instrument_usage_tallies_every_create_call() -> None:
    # RAGAS fait N appels internes: l'instrumentation doit tous les capter.
    from src.goldset.eval import _instrument_usage, _TokenUsage

    class _Usage:
        def __init__(self, p: int, c: int) -> None:
            self.prompt_tokens = p
            self.completion_tokens = c
            self.cost = 0.001

    class _Resp:
        def __init__(self, p: int, c: int) -> None:
            self.usage = _Usage(p, c)

    class _Completions:
        def create(self, **kwargs: object) -> _Resp:
            return _Resp(10, 3)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    client = _Client()
    tracker = _TokenUsage()
    assert _instrument_usage(client, tracker) is True
    client.chat.completions.create(model="x")
    client.chat.completions.create(model="x")

    assert (tracker.prompt_tokens, tracker.completion_tokens, tracker.calls, tracker.attempted_calls) == (20, 6, 2, 2)
    usage = tracker.as_dict("llama-3.3-70b-instruct", "scaleway", capture_complete=True)
    assert usage["cost_eur"] == round(20 / 1e6 * 0.9 + 6 / 1e6 * 0.9, 6)
    assert usage["reported_cost"] == 0.002


def test_instrument_usage_distinguishes_missing_usage_payload() -> None:
    from src.goldset.eval import _instrument_usage, _TokenUsage

    class _Completions:
        def create(self, **kwargs: object) -> object:
            return object()

    class _Client:
        chat = type("_Chat", (), {"completions": _Completions()})()

    tracker = _TokenUsage()
    client = _Client()
    assert _instrument_usage(client, tracker) is True
    client.chat.completions.create(model="x")

    assert tracker.attempted_calls == 1
    assert tracker.calls == 0


def test_token_usage_keeps_openrouter_credits_separate_from_eur() -> None:
    from src.goldset.eval import _TokenUsage

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 10
        cost = 0.0035

    tracker = _TokenUsage()
    assert tracker.record(_Usage()) is True

    usage = tracker.as_dict("x-ai/grok-4.5", "openrouter", capture_complete=True)
    assert usage["cost_eur"] is None
    assert usage["reported_cost"] == 0.0035
    assert usage["reported_cost_unit"] == "openrouter_credit"


def test_aggregate_token_usage_exact_billable_plus_free_estimate() -> None:
    from src.goldset.eval import EvalItem, _aggregate_token_usage

    item = EvalItem(question_id=1, question="q", gold_answer="g", gold_sources=[])
    item.judge_result = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "calls": 1,
            "model": "qwen3-235b-a22b-instruct-2507",
            "provider": "scaleway",
            "cost_eur": 0.0012,
            "capture_complete": True,
        },
    }
    item.ragas_metrics = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 500,
            "calls": 8,
            "model": "llama-3.3-70b-instruct",
            "provider": "scaleway",
            "cost_eur": 0.00495,
            "capture_complete": True,
        },
    }
    item.timing = {"response_length_tokens": 300}
    item.contexts = [{"content": "x" * 40_000, "metadata_not_sent": "n" * 40_000}]
    item.metadata = {
        "generator_prompt_chars": 400,
        "selector_prompt_chars": 800,
        "selector_response_chars": 40,
    }

    out = _aggregate_token_usage([item])

    assert out["judge"]["prompt_tokens"] == 1000
    assert out["ragas"]["calls"] == 8
    assert out["billable_cost_eur"] == round(0.0012 + 0.00495, 4)
    # générateur Albert (gratuit): sortie = compteur réel, coût nul
    assert out["generator_albert_est"]["prompt_tokens"] == 100
    assert out["generator_albert_est"]["completion_tokens"] == 300
    assert out["generator_albert_est"]["cost_eur"] == 0.0
    assert out["selector_albert_est"]["prompt_tokens"] == 200
    assert out["selector_albert_est"]["completion_tokens"] == 10


def test_aggregate_token_usage_fails_closed_for_unknown_eur_cost() -> None:
    from src.goldset.eval import EvalItem, _aggregate_token_usage

    item = EvalItem(question_id=1, question="q", gold_answer="g", gold_sources=[])
    item.judge_result = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "calls": 1,
            "model": "x-ai/grok-4.5",
            "provider": "openrouter",
            "cost_eur": None,
            "reported_cost": 0.0042,
            "reported_cost_unit": "openrouter_credit",
            "capture_complete": True,
        },
    }
    item.ragas_metrics = {"status": "skipped", "reason": "disabled"}

    out = _aggregate_token_usage([item])

    assert out["billable_cost_eur"] is None
    assert out["judge"]["reported_cost"] == 0.0042
    assert out["judge"]["reported_cost_unit"] == "openrouter_credit"
    assert out["judge"]["coverage_complete"] is True


def test_aggregate_token_usage_fails_closed_for_partial_reported_cost() -> None:
    from src.goldset.eval import EvalItem, _aggregate_token_usage

    captured = EvalItem(question_id=1, question="q", gold_answer="g", gold_sources=[])
    captured.judge_result = {
        "status": "completed",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "calls": 1,
            "model": "x-ai/grok-4.5",
            "provider": "openrouter",
            "cost_eur": None,
            "reported_cost": 0.0042,
            "reported_cost_unit": "openrouter_credit",
            "capture_complete": True,
        },
    }
    captured.ragas_metrics = {"status": "skipped", "reason": "disabled"}

    incomplete = EvalItem(question_id=2, question="q", gold_answer="g", gold_sources=[])
    incomplete.judge_result = {
        "status": "failed",
        "reason": "provider timeout",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "calls": 0,
            "model": "x-ai/grok-4.5",
            "provider": "openrouter",
            "cost_eur": None,
            "reported_cost": None,
            "reported_cost_unit": None,
            "capture_complete": False,
        },
    }
    incomplete.ragas_metrics = {"status": "skipped", "reason": "disabled"}

    out = _aggregate_token_usage([captured, incomplete])

    assert out["judge"]["coverage_complete"] is False
    assert out["judge"]["reported_cost"] is None
    assert out["judge"]["reported_cost_unit"] is None


def test_aggregate_token_usage_fails_closed_when_capture_is_missing() -> None:
    from src.goldset.eval import EvalItem, _aggregate_token_usage

    item = EvalItem(question_id=1, question="q", gold_answer="g", gold_sources=[])
    item.judge_result = {"status": "failed", "reason": "provider timeout"}
    item.ragas_metrics = {"status": "skipped", "reason": "disabled"}

    out = _aggregate_token_usage([item])

    assert out["billable_cost_eur"] is None
    assert out["judge"]["coverage_complete"] is False


def test_run_question_records_exact_generator_prompt_size() -> None:
    from types import SimpleNamespace

    from src.goldset.eval import GoldsetQuestion, run_question

    class _Pipe:
        last_full_prompt = "user prompt"
        last_system_prompt = "system"

        def run_with_trace(self, *args, **kwargs):
            return SimpleNamespace(
                answer="answer",
                context_items=[],
                sources=[],
                timing={"response_length_tokens": 2},
                metadata={"existing": True},
            )

    item = run_question(
        pipe=_Pipe(),
        question=GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[]),
        run_ragas=False,
        run_judge=False,
        judge_model="",
        judge_base_url="",
        judge_api_key="",
        ragas_model="",
        scaleway_base_url="",
        scaleway_api_key="",
    )

    assert item.metadata["generator_prompt_chars"] == len("user promptsystem")
    assert item.metadata["existing"] is True


def test_judge_answer_openrouter_enforces_zdr(monkeypatch) -> None:
    """Revue #329 : data_collection=deny n'exclut que les providers qui
    collectent/entraînent — la ZDR est un attribut distinct chez OpenRouter,
    le payload doit donc exiger LES DEUX."""
    from src.goldset import eval as eval_module

    captured: dict = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Msg:
                content = '{"pass": true, "score": 1.0, "rationale": "ok"}'

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = _FakeChat()

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    result = eval_module.judge_answer(
        question="q",
        gold_answer="a",
        answer="b",
        contexts=[],
        deterministic_metrics={},
        model="m",
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        provider="openrouter",
    )
    assert result["status"] == "completed"
    assert captured["extra_body"] == {"provider": {"data_collection": "deny", "zdr": True}}


def test_run_question_reraises_db_connection_errors() -> None:
    """Revue #329 : les coupures de connexion doivent REMONTER au retry du
    runner au lieu d'être absorbées dans item.error."""
    import psycopg

    from src.goldset.eval import GoldsetQuestion, run_question

    class _DeadPipe:
        def run_with_trace(self, *args, **kwargs):
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    q = GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[])
    with pytest.raises(psycopg.OperationalError):
        run_question(
            pipe=_DeadPipe(),
            question=q,
            run_ragas=False,
            run_judge=False,
            judge_model="",
            judge_base_url="",
            judge_api_key="",
            ragas_model="",
            scaleway_base_url="",
            scaleway_api_key="",
        )


def test_run_question_with_retry_three_attempts(monkeypatch) -> None:
    """Revue #329 : preuve que 3 tentatives ont bien lieu, puis propagation."""
    import psycopg

    from src.goldset import eval as eval_module

    q = eval_module.GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[])
    sentinel = eval_module.EvalItem(question_id=1, question="q", gold_answer="a", gold_sources=[])
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.OperationalError("boom")
        return sentinel

    monkeypatch.setattr(eval_module, "run_question", flaky)
    assert eval_module.run_question_with_retry(backoff_s=0, question=q) is sentinel
    assert calls["n"] == 3

    calls["n"] = 0

    def always_dead(**kwargs):
        calls["n"] += 1
        raise psycopg.InterfaceError("dead")

    monkeypatch.setattr(eval_module, "run_question", always_dead)
    with pytest.raises(psycopg.InterfaceError):
        eval_module.run_question_with_retry(backoff_s=0, question=q)
    assert calls["n"] == 3


def test_run_question_absorbs_query_canceled_without_retry() -> None:
    """Revue #331 : QueryCanceled (sous-classe d'OperationalError) est une
    erreur de REQUÊTE, pas de connexion — absorbée en item.error, jamais
    rejouée, le run continue."""
    import psycopg

    from src.goldset.eval import GoldsetQuestion, run_question_with_retry

    calls = {"n": 0}

    class _TimeoutPipe:
        def run_with_trace(self, *args, **kwargs):
            calls["n"] += 1
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    q = GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[])
    item = run_question_with_retry(
        backoff_s=0,
        pipe=_TimeoutPipe(),
        question=q,
        run_ragas=False,
        run_judge=False,
        judge_model="",
        judge_base_url="",
        judge_api_key="",
        ragas_model="",
        scaleway_base_url="",
        scaleway_api_key="",
    )
    assert calls["n"] == 1  # pas de retry : l'annulation n'est pas une coupure
    assert item.error is not None and "timeout" in item.error


def test_judge_answer_with_votes_majority(monkeypatch) -> None:
    """Vote majoritaire : 2 pass / 1 fail -> PASS, votes archivés, accord 2/3."""
    from src.goldset import eval as eval_module

    seq = iter(
        [
            {"status": "completed", "pass": True, "score": 0.9, "failure_category": None},
            {"status": "completed", "pass": False, "score": 0.4, "failure_category": "incomplete"},
            {"status": "completed", "pass": True, "score": 0.8, "failure_category": None},
        ]
    )
    monkeypatch.setattr(eval_module, "judge_answer", lambda **kwargs: next(seq))
    result = eval_module.judge_answer_with_votes(votes=3, question="q")
    assert result["pass"] is True
    assert len(result["votes"]) == 3
    assert result["vote_agreement"] == "2/3"
    # payload de base cohérent avec le verdict (un vote majoritaire)
    assert result["failure_category"] is None


def test_judge_answer_with_votes_sums_usage_from_every_paid_call(monkeypatch) -> None:
    from src.goldset import eval as eval_module

    def usage(prompt: int, completion: int, cost: float) -> dict:
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "calls": 1,
            "model": "qwen3-235b-a22b-instruct-2507",
            "provider": "scaleway",
            "cost_eur": cost,
            "reported_cost": None,
            "reported_cost_unit": None,
            "capture_complete": True,
        }

    seq = iter(
        [
            {"status": "completed", "pass": True, "score": 0.9, "usage": usage(100, 10, 0.000097)},
            {"status": "completed", "pass": False, "score": 0.4, "usage": usage(110, 11, 0.000107)},
            {"status": "completed", "pass": True, "score": 0.8, "usage": usage(120, 12, 0.000117)},
        ]
    )
    monkeypatch.setattr(eval_module, "judge_answer", lambda **kwargs: next(seq))

    result = eval_module.judge_answer_with_votes(
        votes=3,
        question="q",
        provider="scaleway",
        model="qwen3-235b-a22b-instruct-2507",
    )

    assert result["pass"] is True
    assert result["usage"]["prompt_tokens"] == 330
    assert result["usage"]["completion_tokens"] == 33
    assert result["usage"]["calls"] == 3
    assert result["usage"]["cost_eur"] == 0.000321
    assert result["usage"]["capture_complete"] is True
    assert [vote["usage"]["prompt_tokens"] for vote in result["votes"]] == [100, 110, 120]


def test_judge_answer_with_votes_tolerates_a_failed_vote(monkeypatch) -> None:
    """Un vote en erreur (ex. 429) n'invalide pas le verdict : majorité des complétés."""
    from src.goldset import eval as eval_module

    seq = iter(
        [
            {"status": "failed", "reason": "429"},
            {"status": "completed", "pass": False, "score": 0.3, "failure_category": "wrong_law"},
            {"status": "completed", "pass": False, "score": 0.2, "failure_category": "wrong_law"},
        ]
    )
    monkeypatch.setattr(eval_module, "judge_answer", lambda **kwargs: next(seq))
    result = eval_module.judge_answer_with_votes(votes=3, question="q")
    assert result["pass"] is False
    assert result["vote_agreement"] == "2/2"
    assert result["status"] == "completed"


def test_judge_answer_with_votes_requires_original_majority_quorum(monkeypatch) -> None:
    """Deux erreurs ne doivent jamais transformer un maj-3 en single-shot."""
    from src.goldset import eval as eval_module

    seq = iter(
        [
            {"status": "failed", "reason": "429"},
            {"status": "failed", "reason": "timeout"},
            {"status": "completed", "pass": True, "score": 0.9},
        ]
    )
    monkeypatch.setattr(eval_module, "judge_answer", lambda **kwargs: next(seq))

    result = eval_module.judge_answer_with_votes(votes=3, question="q")

    assert result["status"] == "failed"
    assert "required=2" in result["reason"]
    assert result["vote_agreement"] == "1/1"
    assert [vote["reason"] for vote in result["votes"]] == ["429", "timeout", None]


def test_judge_answer_with_votes_rejects_tie_after_failed_vote(monkeypatch) -> None:
    """Un split 1-1 après une erreur n'est pas une majorité et doit échouer."""
    from src.goldset import eval as eval_module

    seq = iter(
        [
            {"status": "failed", "reason": "429"},
            {"status": "completed", "pass": True, "score": 0.9},
            {"status": "completed", "pass": False, "score": 0.2},
        ]
    )
    monkeypatch.setattr(eval_module, "judge_answer", lambda **kwargs: next(seq))

    result = eval_module.judge_answer_with_votes(votes=3, question="q")

    assert result["status"] == "failed"
    assert "pass=1, fail=1" in result["reason"]
    assert result["vote_agreement"] == "1/2"


def test_judge_answer_with_votes_single_is_passthrough(monkeypatch) -> None:
    """votes=1 = single-shot exact (screening intermédiaire) : ni votes ni agrément."""
    from src.goldset import eval as eval_module

    calls = {"n": 0}

    def one(**kwargs):
        calls["n"] += 1
        return {"status": "completed", "pass": True, "score": 1.0}

    monkeypatch.setattr(eval_module, "judge_answer", one)
    result = eval_module.judge_answer_with_votes(votes=1, question="q")
    assert calls["n"] == 1
    assert "votes" not in result and "vote_agreement" not in result


def _load_rag_quality_protocol_module():
    script_path = Path(__file__).parents[1] / ".github/scripts/rag_quality_protocol.py"
    spec = importlib.util.spec_from_file_location("rag_quality_protocol", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"event_name": "pull_request", "pr_full_requested": False},
            ("openrouter", "", 1, False),
        ),
        (
            {"event_name": "pull_request", "pr_full_requested": True},
            ("scaleway", "qwen3-235b-a22b-instruct-2507", 3, True),
        ),
        (
            {"event_name": "workflow_dispatch", "eval_mode": "smoke", "target_environment": "staging"},
            ("openrouter", "", 1, False),
        ),
        (
            {"event_name": "workflow_dispatch", "eval_mode": "full", "target_environment": "staging"},
            ("scaleway", "qwen3-235b-a22b-instruct-2507", 3, True),
        ),
        (
            {"event_name": "workflow_dispatch", "eval_mode": "smoke", "target_environment": "production"},
            ("scaleway", "qwen3-235b-a22b-instruct-2507", 3, True),
        ),
    ],
)
def test_rag_quality_protocol_event_matrix(kwargs, expected) -> None:
    protocol_module = _load_rag_quality_protocol_module()

    protocol = protocol_module.resolve_protocol(**kwargs)

    assert (protocol.provider, protocol.model, protocol.votes, protocol.official) == expected


@pytest.mark.parametrize(
    "forbidden",
    [
        {"skip_judge": True},
        {"requested_model": "another-model"},
    ],
)
def test_rag_quality_official_protocol_rejects_overrides(forbidden) -> None:
    protocol_module = _load_rag_quality_protocol_module()

    with pytest.raises(ValueError, match="official adoption gate"):
        protocol_module.resolve_protocol(
            event_name="workflow_dispatch",
            eval_mode="full",
            target_environment="staging",
            **forbidden,
        )


def _attempt_metadata() -> dict:
    return {
        "retrieval_attempts": [
            {
                "name": "initial",
                "chunks_before_rerank": [
                    {"doc_id": "gold-doc", "chunk_id": "c1"},
                    {"doc_id": "noise-doc", "chunk_id": "c2"},
                ],
                "aggregated_sections": [
                    {"section_id": "s-noise", "document_id": "noise-doc", "score": 0.9},
                    {"section_id": "s-gold", "document_id": "gold-doc", "score": 0.8},
                ],
                "selector": {
                    "decisions": {
                        "kept": [{"idx": 0, "document_id": "noise-doc"}],
                        "removed": [{"idx": 1, "document_id": "gold-doc"}],
                    }
                },
            }
        ]
    }


def test_stage_retrieval_metrics_localizes_gold_loss() -> None:
    """Le funnel doit montrer où le gold est perdu (ici : au selector)."""
    stages = stage_retrieval_metrics(_attempt_metadata(), ["gold-doc"])

    initial = stages["initial"]
    assert initial["pool"]["hit_rate"] == 1.0
    assert initial["sections_top12"]["hit_rate"] == 1.0
    assert initial["sections_top20"]["hit_rate"] == 1.0
    assert initial["selector_kept"]["hit_rate"] == 0.0
    assert initial["selector_kept"]["doc_recall"] == 0.0


def test_stage_retrieval_metrics_kept_falls_back_to_idx() -> None:
    metadata = _attempt_metadata()
    kept = metadata["retrieval_attempts"][0]["selector"]["decisions"]["kept"]
    kept[0] = {"idx": 1}  # entrée historique sans document_id
    stages = stage_retrieval_metrics(metadata, ["gold-doc"])
    assert stages["initial"]["selector_kept"]["hit_rate"] == 1.0


def test_stage_retrieval_metrics_without_attempts() -> None:
    assert stage_retrieval_metrics({}, ["gold-doc"]) == {}


def test_aggregate_items_averages_stage_metrics() -> None:
    def item(hit: float) -> EvalItem:
        it = EvalItem(question_id=1, question="q", gold_answer="a", gold_sources=["g"])
        it.deterministic_metrics = {
            "hit_rate": hit,
            "stages": {"initial": {"pool": {"hit_rate": hit, "doc_recall": hit, "doc_count": 2}}},
        }
        return it

    aggregate = aggregate_items([item(1.0), item(0.0)])

    pool = aggregate["stage_metrics"]["initial"]["pool"]
    assert pool == {"n": 2, "hit_rate_avg": 0.5, "doc_recall_avg": 0.5}


def test_borderline_double_read_helpers() -> None:
    """La double lecture exclut les questions au juge instable sans toucher
    au taux officiel (le gate reste sur le taux complet)."""
    q_stable = GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[], tags=["baseline_v1"])
    q_flaky = GoldsetQuestion(id=2, question="q", gold_answer="a", gold_sources=[], tags=["baseline_v1", "juge_borderline"])
    assert borderline_question_ids([q_stable, q_flaky]) == [2]

    def item(qid: int, ok: bool) -> EvalItem:
        it = EvalItem(question_id=qid, question="q", gold_answer="a", gold_sources=[])
        it.judge_result = {"status": "completed", "pass": ok}
        return it

    items = [item(1, True), item(2, False), item(3, False)]
    assert judge_pass_rate_excluding(items, set()) == 1 / 3
    assert judge_pass_rate_excluding(items, {2}) == 0.5
    assert judge_pass_rate_excluding(items, {1, 2, 3}) is None
