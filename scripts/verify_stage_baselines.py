#!/usr/bin/env python3
"""Verify or exactly compare a versioned stage-replay bundle offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SCHEMA_VERSION = "m0-replay-v1"
STAGE_CONTRACTS = {
    "01_query_processor.json": "query-processor.schema.json",
    "02_retriever.json": "retriever.schema.json",
    "03_section_aggregator.json": "section-aggregator.schema.json",
    "04_context_selector.json": "context-selector.schema.json",
    "05_context_builder.json": "context-builder.schema.json",
    "06_generator.json": "generator.schema.json",
}
REQUIRED_FIXTURE_FILES = ("00_input.json", *STAGE_CONTRACTS, "07_pipeline_result.json")
PERSONAL_DATA_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "french_phone": re.compile(r"(?<!\d)(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}(?!\d)"),
    "nir": re.compile(r"(?<!\d)[12]\s?\d{2}\s?(?:0[1-9]|1[0-2])\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}(?!\d)"),
}


class ReplayVerificationError(RuntimeError):
    """Raised when a replay bundle is incomplete, changed, or non-conformant."""


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayVerificationError(f"Cannot read valid JSON from {path}: {exc}") from exc


def _safe_artifact_path(root: Path, relative_path: str) -> Path:
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ReplayVerificationError(f"Unsafe artifact path in manifest: {relative_path!r}")
    return root.joinpath(*pure_path.parts)


def _verify_input_has_no_personal_data(path: Path, payload: dict[str, Any]) -> None:
    values = [str(payload.get("query") or "")]
    for message in payload.get("conversation_history") or []:
        if isinstance(message, dict):
            values.append(str(message.get("content") or ""))
    text = "\n".join(values)
    matches = [name for name, pattern in PERSONAL_DATA_PATTERNS.items() if pattern.search(text)]
    if matches:
        raise ReplayVerificationError(f"Possible personal data in {path}: {', '.join(matches)}")


def _verify_pipeline_result(path: Path, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ReplayVerificationError(f"Pipeline result must be an object: {path}")
    expected_types = {"query": str, "answer": str, "sources": list, "metadata": dict}
    for key, expected_type in expected_types.items():
        if not isinstance(payload.get(key), expected_type):
            raise ReplayVerificationError(f"Pipeline result {path} has invalid {key!r}")


def _verify_schema(path: Path, contract_path: Path, payload: Any) -> None:
    contract = _load_json(contract_path)
    errors = sorted(Draft202012Validator(contract).iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        details = "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors[:5])
        raise ReplayVerificationError(f"Schema validation failed for {path}: {details}")


def _verify_fingerprint(manifest: dict[str, Any]) -> None:
    expected = str(manifest.get("replay_fingerprint") or "")
    payload = {key: value for key, value in manifest.items() if key not in {"generated_at", "replay_fingerprint"}}
    actual = _json_sha256(payload)
    if not expected or actual != expected:
        raise ReplayVerificationError(f"Replay fingerprint mismatch: expected {expected!r}, got {actual!r}")


def _verify_declared_coverage(manifest: dict[str, Any], input_payloads: dict[str, dict[str, Any]]) -> None:
    coverage = manifest.get("coverage")
    if not isinstance(coverage, list):
        raise ReplayVerificationError("Replay manifest coverage must be a list")
    coverage_by_id = {
        str(item.get("id")): item
        for item in coverage
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if set(coverage_by_id) != set(input_payloads) or len(coverage_by_id) != len(coverage):
        raise ReplayVerificationError("Coverage fixture IDs do not match replay inputs")

    for fixture_id, input_payload in input_payloads.items():
        item = coverage_by_id[fixture_id]
        expected = input_payload.get("expected") or {}
        observed = item.get("observed") or {}
        if item.get("expected") != expected:
            raise ReplayVerificationError(f"Coverage expectation differs from input contract for {fixture_id}")
        mismatches = [key for key, value in expected.items() if observed.get(key) != value]
        if mismatches:
            raise ReplayVerificationError(f"Coverage mismatch for {fixture_id}: {', '.join(sorted(mismatches))}")


def _compare_actual(expected_dir: Path, actual_dir: Path, artifact_paths: list[str]) -> None:
    differences = []
    expected_inventory = set(artifact_paths)
    actual_inventory = {
        path.relative_to(actual_dir).as_posix()
        for path in actual_dir.rglob("*.json")
        if path != actual_dir / "manifest.json"
    }
    missing_paths = sorted(expected_inventory - actual_inventory)
    unexpected_paths = sorted(actual_inventory - expected_inventory)
    differences.extend(f"missing {path}" for path in missing_paths)
    differences.extend(f"unexpected {path}" for path in unexpected_paths)

    for relative_path in artifact_paths:
        expected_path = _safe_artifact_path(expected_dir, relative_path)
        actual_path = _safe_artifact_path(actual_dir, relative_path)
        if not actual_path.is_file():
            continue
        expected_payload = _load_json(expected_path)
        actual_payload = _load_json(actual_path)
        expected_json = json.dumps(expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual_json = json.dumps(actual_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if actual_json != expected_json:
            differences.append(relative_path)
    if differences:
        raise ReplayVerificationError("Exact replay comparison failed: " + ", ".join(differences[:20]))


def verify_baseline(baseline_dir: Path, actual_dir: Path | None = None) -> dict[str, Any]:
    baseline_dir = baseline_dir.resolve()
    manifest_path = baseline_dir / "manifest.json"
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ReplayVerificationError(f"Manifest must be an object: {manifest_path}")
    if manifest.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ReplayVerificationError(
            f"Unsupported replay schema {manifest.get('schema_version')!r}; expected {EXPECTED_SCHEMA_VERSION!r}"
        )
    if manifest.get("errors") or manifest.get("coverage_errors") or manifest.get("failed_count"):
        raise ReplayVerificationError("Replay manifest records generation or coverage errors")

    reference_run_id = manifest.get("reference_run_id")
    reference_run = manifest.get("reference_run")
    if reference_run_id is None and reference_run is None:
        pass
    elif not isinstance(reference_run, dict) or reference_run.get("id") != reference_run_id:
        raise ReplayVerificationError("Replay manifest has an inconsistent live reference run")
    else:
        if reference_run.get("status") != "completed":
            raise ReplayVerificationError("Replay manifest live reference run is not completed")
        if reference_run.get("git_sha") != manifest.get("git_commit_sha"):
            raise ReplayVerificationError("Live reference run and replay bundle use different Git revisions")
        if reference_run.get("pipeline_config_fingerprint") != manifest.get("pipeline_config_fingerprint"):
            raise ReplayVerificationError("Live reference run and replay bundle use different pipeline configurations")

    artifact_hashes = manifest.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or not artifact_hashes:
        raise ReplayVerificationError("Replay manifest has no artifact hashes")

    fixture_ids = sorted({PurePosixPath(path).parts[0] for path in artifact_hashes})
    if len(fixture_ids) != manifest.get("query_count") or len(fixture_ids) != manifest.get("succeeded_count"):
        raise ReplayVerificationError("Fixture count does not match manifest query/success counts")

    expected_artifact_paths = {
        f"{fixture_id}/{filename}"
        for fixture_id in fixture_ids
        for filename in REQUIRED_FIXTURE_FILES
    }
    if set(artifact_hashes) != expected_artifact_paths:
        raise ReplayVerificationError("Artifact inventory does not exactly match the fixture contract")
    disk_artifact_paths = {
        path.relative_to(baseline_dir).as_posix()
        for path in baseline_dir.rglob("*.json")
        if path != manifest_path
    }
    if disk_artifact_paths != expected_artifact_paths:
        raise ReplayVerificationError("Files on disk do not exactly match the manifest artifact inventory")

    contracts_dir = REPO_ROOT / "tests/conformance/contracts"
    input_payloads: dict[str, dict[str, Any]] = {}
    for fixture_id in fixture_ids:
        for filename in REQUIRED_FIXTURE_FILES:
            relative_path = f"{fixture_id}/{filename}"
            expected_hash = artifact_hashes.get(relative_path)
            if not expected_hash:
                raise ReplayVerificationError(f"Manifest is missing required artifact {relative_path}")
            path = _safe_artifact_path(baseline_dir, relative_path)
            if not path.is_file():
                raise ReplayVerificationError(f"Missing replay artifact: {path}")
            actual_hash = _file_sha256(path)
            if actual_hash != expected_hash:
                raise ReplayVerificationError(f"Artifact hash mismatch for {relative_path}: expected {expected_hash}, got {actual_hash}")
            payload = _load_json(path)
            if filename == "00_input.json":
                if not isinstance(payload, dict):
                    raise ReplayVerificationError(f"Input fixture must be an object: {path}")
                _verify_input_has_no_personal_data(path, payload)
                input_payloads[fixture_id] = payload
            elif filename == "07_pipeline_result.json":
                _verify_pipeline_result(path, payload)
            else:
                _verify_schema(path, contracts_dir / STAGE_CONTRACTS[filename], payload)

    _verify_declared_coverage(manifest, input_payloads)
    _verify_fingerprint(manifest)
    sorted_artifact_paths = sorted(str(path) for path in artifact_hashes)
    if actual_dir is not None:
        _compare_actual(baseline_dir, actual_dir.resolve(), sorted_artifact_paths)

    return {
        "status": "ok",
        "baseline_dir": str(baseline_dir),
        "fixture_count": len(fixture_ids),
        "artifact_count": len(artifact_hashes),
        "replay_fingerprint": manifest["replay_fingerprint"],
        "exact_comparison": str(actual_dir.resolve()) if actual_dir is not None else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify or exactly compare deterministic stage replay artifacts.")
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--actual-dir", type=Path, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = verify_baseline(args.baseline_dir, args.actual_dir)
    except ReplayVerificationError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
