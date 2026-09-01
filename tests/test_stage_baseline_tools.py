from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.capture_rag_parity_evidence import capture_evidence
from scripts.dump_stage_baselines import _assert_no_personal_data, _stable_pipeline_metadata
from scripts.verify_stage_baselines import ReplayVerificationError, _compare_actual, _verify_declared_coverage, verify_baseline


def test_stable_pipeline_metadata_excludes_request_ids_and_timing() -> None:
    metadata = {
        "intent": "rag_query",
        "tables_searched": ["matte", "service_public"],
        "turn_id": "ephemeral-turn",
        "trace_id": "ephemeral-trace",
        "rag_trace_events": [{"duration_ms": 12.5}],
    }

    assert _stable_pipeline_metadata(metadata) == {
        "intent": "rag_query",
        "tables_searched": ["matte", "service_public"],
    }


def test_fixture_personal_data_guard_rejects_email() -> None:
    with pytest.raises(ValueError, match="possible personal data: email"):
        _assert_no_personal_data(
            {
                "id": "unsafe",
                "query": "Le dossier de agent@example.gouv.fr est-il complet ?",
                "conversation_history": [],
            }
        )


def test_exact_replay_comparison_detects_structured_difference(tmp_path: Path) -> None:
    expected_dir = tmp_path / "expected"
    actual_dir = tmp_path / "actual"
    expected_path = expected_dir / "fixture" / "07_pipeline_result.json"
    actual_path = actual_dir / "fixture" / "07_pipeline_result.json"
    expected_path.parent.mkdir(parents=True)
    actual_path.parent.mkdir(parents=True)
    expected_path.write_text(json.dumps({"answer": "référence", "sources": []}), encoding="utf-8")
    actual_path.write_text(json.dumps({"answer": "différent", "sources": []}), encoding="utf-8")

    with pytest.raises(ReplayVerificationError, match="Exact replay comparison failed"):
        _compare_actual(expected_dir, actual_dir, ["fixture/07_pipeline_result.json"])


def test_declared_coverage_must_match_input_expectations() -> None:
    manifest = {
        "coverage": [
            {
                "id": "fixture",
                "expected": {"intent": "chit_chat"},
                "observed": {"intent": "rag_query"},
            }
        ]
    }
    inputs = {"fixture": {"expected": {"intent": "chit_chat"}}}

    with pytest.raises(ReplayVerificationError, match="Coverage mismatch"):
        _verify_declared_coverage(manifest, inputs)


def test_versioned_m0_replay_bundle_is_complete() -> None:
    baseline_dir = Path(__file__).parent / "conformance/baselines/m0-api-parity-dev-9bf1cf0"

    report = verify_baseline(baseline_dir)

    assert report["fixture_count"] == 7
    assert report["artifact_count"] == 56


def test_parity_evidence_rejects_incomplete_run() -> None:
    class StubResult:
        def fetchone(self) -> dict[str, object]:
            return {"id": 999, "status": "running"}

    class StubConnection:
        def execute(self, _query: str, _params: tuple[int]) -> StubResult:
            return StubResult()

    with pytest.raises(RuntimeError, match="not completed"):
        capture_evidence(StubConnection(), 999, None, None)  # type: ignore[arg-type]
