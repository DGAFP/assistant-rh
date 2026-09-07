from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import scripts.capture_rag_parity_evidence as evidence_tools
import scripts.dump_stage_baselines as dump_tools
from scripts.capture_rag_parity_evidence import build_parser as build_evidence_parser
from scripts.capture_rag_parity_evidence import capture_evidence
from scripts.dump_stage_baselines import (
    _assert_no_personal_data,
    _load_queries_from_goldset,
    _prepare_output_dir,
    _reference_run_snapshot,
    _stable_pipeline_metadata,
)
from scripts.verify_stage_baselines import (
    ReplayVerificationError,
    _compare_actual,
    _json_sha256,
    _verify_declared_coverage,
    verify_baseline,
)


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


def test_goldset_queries_also_use_personal_data_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: list[Any]) -> None:
            return None

        def fetchall(self) -> list[dict[str, Any]]:
            return [{"id": 7, "question": "Écrire à agent@example.gouv.fr", "goldset_name": "test", "tags": []}]

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

    monkeypatch.setattr(dump_tools, "get_dsn", lambda: "postgresql://example")
    monkeypatch.setattr(dump_tools.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())

    with pytest.raises(ValueError, match="possible personal data: email"):
        _load_queries_from_goldset(["test"], None)


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


def test_exact_replay_comparison_is_type_sensitive(tmp_path: Path) -> None:
    expected_dir = tmp_path / "expected"
    actual_dir = tmp_path / "actual"
    expected_path = expected_dir / "fixture" / "01_query_processor.json"
    actual_path = actual_dir / "fixture" / "01_query_processor.json"
    expected_path.parent.mkdir(parents=True)
    actual_path.parent.mkdir(parents=True)
    expected_path.write_text(json.dumps({"output": {"should_proceed": True}}), encoding="utf-8")
    actual_path.write_text(json.dumps({"output": {"should_proceed": 1}}), encoding="utf-8")

    with pytest.raises(ReplayVerificationError, match="Exact replay comparison failed"):
        _compare_actual(expected_dir, actual_dir, ["fixture/01_query_processor.json"])


def test_exact_replay_comparison_rejects_unexpected_artifacts(tmp_path: Path) -> None:
    expected_dir = tmp_path / "expected"
    actual_dir = tmp_path / "actual"
    expected_path = expected_dir / "fixture" / "07_pipeline_result.json"
    actual_path = actual_dir / "fixture" / "07_pipeline_result.json"
    unexpected_path = actual_dir / "extra" / "07_pipeline_result.json"
    expected_path.parent.mkdir(parents=True)
    actual_path.parent.mkdir(parents=True)
    unexpected_path.parent.mkdir(parents=True)
    payload = json.dumps({"answer": "identique"})
    expected_path.write_text(payload, encoding="utf-8")
    actual_path.write_text(payload, encoding="utf-8")
    unexpected_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ReplayVerificationError, match="unexpected extra/07_pipeline_result.json"):
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


def test_standalone_replay_bundle_without_live_reference_is_valid(tmp_path: Path) -> None:
    source_dir = Path(__file__).parent / "conformance/baselines/m0-api-parity-dev-9bf1cf0"
    baseline_dir = tmp_path / "standalone"
    shutil.copytree(source_dir, baseline_dir)
    manifest_path = baseline_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reference_run_id"] = None
    manifest["reference_run"] = None
    fingerprint_payload = {key: value for key, value in manifest.items() if key not in {"generated_at", "replay_fingerprint"}}
    manifest["replay_fingerprint"] = _json_sha256(fingerprint_payload)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report = verify_baseline(baseline_dir)

    assert report["status"] == "ok"


def test_replay_bundle_rejects_incomplete_live_reference(tmp_path: Path) -> None:
    source_dir = Path(__file__).parent / "conformance/baselines/m0-api-parity-dev-9bf1cf0"
    baseline_dir = tmp_path / "incomplete-reference"
    shutil.copytree(source_dir, baseline_dir)
    manifest_path = baseline_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reference_run"]["status"] = "running"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ReplayVerificationError, match="live reference run is not completed"):
        verify_baseline(baseline_dir)


def test_recorder_rejects_incomplete_live_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResult:
        def fetchone(self) -> dict[str, Any]:
            return {"id": 240, "status": "failed"}

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: tuple[int]) -> FakeResult:
            return FakeResult()

    monkeypatch.setattr(dump_tools, "get_dsn", lambda: "postgresql://example")
    monkeypatch.setattr(dump_tools.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())

    with pytest.raises(RuntimeError, match="not completed"):
        _reference_run_snapshot(240)


def test_output_directory_must_be_empty_or_a_replaceable_replay(tmp_path: Path) -> None:
    output_dir = tmp_path / "replay"
    output_dir.mkdir()
    unrelated = output_dir / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    with pytest.raises(SystemExit, match="not empty"):
        _prepare_output_dir(output_dir, replace=False)
    with pytest.raises(SystemExit, match="without a replay manifest"):
        _prepare_output_dir(output_dir, replace=True)
    assert unrelated.read_text(encoding="utf-8") == "keep"

    (output_dir / "manifest.json").write_text(json.dumps({"query_count": 1}), encoding="utf-8")
    _prepare_output_dir(output_dir, replace=True)
    assert output_dir.is_dir()
    assert list(output_dir.iterdir()) == []


def test_evidence_cli_defaults_to_canonical_scaleway_dsn() -> None:
    args = build_evidence_parser().parse_args(["--run-id", "240", "--output", "evidence.json"])

    assert args.dsn_env == "SCW_POSTGRES_DSN"


def test_parity_evidence_rejects_incomplete_run() -> None:
    class StubResult:
        def fetchone(self) -> dict[str, object]:
            return {"id": 999, "status": "running"}

    class StubConnection:
        def execute(self, _query: str, _params: tuple[int]) -> StubResult:
            return StubResult()

    with pytest.raises(RuntimeError, match="not completed"):
        capture_evidence(StubConnection(), 999, None, None)  # type: ignore[arg-type]


def test_parity_evidence_separates_runtime_and_recorder_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_sha = "9bf1cf0cb92420e9e551f811edadb1d7129244b1"
    recorder_sha = "e5b2a9332d464e99180f37ff24e8937508090f5b"

    class StubResult:
        def fetchone(self) -> dict[str, Any]:
            return {
                "id": 240,
                "status": "completed",
                "git_sha": runtime_sha,
                "config": {},
                "metadata": {"eval_scope": {"question_ids": []}},
            }

    class StubConnection:
        def execute(self, _query: str, _params: tuple[int]) -> StubResult:
            return StubResult()

    compared: list[tuple[str, str]] = []
    monkeypatch.setattr(evidence_tools, "get_git_sha", lambda _root: recorder_sha)
    monkeypatch.setattr(
        evidence_tools,
        "assert_runtime_revision_compatible",
        lambda _root, runtime, recorder: compared.append((runtime, recorder)),
    )
    monkeypatch.setattr(evidence_tools, "_db_prompt_snapshot", lambda _conn: [])
    monkeypatch.setattr(evidence_tools, "_local_prompt_snapshot", lambda: [])
    monkeypatch.setattr(evidence_tools, "_corpus_snapshot", lambda _conn: {})
    monkeypatch.setattr(evidence_tools, "_panel_snapshot", lambda _conn, _ids: {})
    monkeypatch.setattr(evidence_tools, "_runtime_outcomes", lambda _conn, _run_id: {})
    monkeypatch.setattr(evidence_tools, "_per_corpus_metrics", lambda _conn, _run_id: [])

    evidence = capture_evidence(StubConnection(), 240, runtime_sha, None)  # type: ignore[arg-type]

    assert evidence["repository"] == {"git_sha": runtime_sha, "recorder_git_sha": recorder_sha}
    assert compared == [(runtime_sha, recorder_sha)]
