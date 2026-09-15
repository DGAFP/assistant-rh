"""Replay published synthetic branches and validate grounding audit provenance.

Grounding judgments are a manual review. These tests check exact quotations and
complete answer coverage, not semantic entailment or universal answer quality.
"""

import hashlib
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / "tests/conformance/companions/c5-acceptance-20260915"
RECORDED = ROOT / "tests/conformance/companions/c5-recorded-20260914"
SPEC = importlib.util.spec_from_file_location("c5_branch_companion", ROOT / "scripts/conformance/c5_branch_companion.py")
companion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(companion)
pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("C5 acceptance evidence must remain offline")

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


async def test_published_synthetic_branches_against_current_core():
    result = await companion.replay(ROOT, EVIDENCE / "synthetic-evidence")
    assert result["exact_comparison"]
    assert len(result["cases"]) == 9
    by_id = {case["id"]: case for case in result["cases"]}
    assert by_id["selector-total-rejection"]["status"] == "all_rejected"
    assert by_id["generator-fallback"]["provider_sequence"] == ["albert", "scaleway"]
    assert by_id["generator-double-outage"]["candidate_failure"]["partial"] is False
    assert by_id["final-rejection-no-answer"]["provider_sequence"] == []


async def test_synthetic_reference_is_reproducible_from_retained_runtime(tmp_path):
    output = tmp_path / "reference"
    companion.record(ROOT, output)
    # This compares reference outputs against frozen evidence, independently of
    # the API replay. Never regenerate/overwrite the published expected values.
    for name in ("prompts.json", "cases.json"):
        assert (output / name).read_bytes() == (EVIDENCE / "synthetic-evidence" / name).read_bytes()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["head"] is None
    assert manifest["source_revision_status"] == "unverified_snapshot"
    assert manifest["source_hashes"]


def altered_evidence(tmp_path, mutation):
    evidence = tmp_path / "altered-evidence"
    shutil.copytree(EVIDENCE / "synthetic-evidence", evidence)
    cases = json.loads((evidence / "cases.json").read_text())
    if mutation == "empty":
        cases = []
    elif mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases[-1] = cases[0]
    elif mutation == "extra":
        cases.append(cases[0])
    elif mutation == "unknown":
        cases[-1]["id"] = "unknown-scenario"
    elif mutation == "wrong-kind":
        cases[-1]["kind"] = "generator"
    elif mutation == "not-a-list":
        cases = {}
    elif mutation == "malformed-entry":
        cases[-1] = None
    elif mutation == "reordered":
        cases.reverse()
    else:
        raise AssertionError(mutation)
    companion.save(evidence / "cases.json", cases)
    manifest = json.loads((evidence / "manifest.json").read_text())
    manifest["files"]["cases.json"] = companion.sha(evidence / "cases.json")
    companion.save(evidence / "manifest.json", manifest)
    return evidence


@pytest.mark.parametrize("mode", ["replay", "check"])
@pytest.mark.parametrize("mutation", ["empty", "missing", "duplicate", "extra", "unknown", "wrong-kind", "not-a-list", "malformed-entry"])
async def test_incomplete_or_invalid_case_panel_is_rejected_before_provider_io(tmp_path, monkeypatch, mode, mutation):
    import httpx

    def unexpected_client(*args, **kwargs):
        pytest.fail("invalid case panels must be rejected before constructing a provider client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    evidence = altered_evidence(tmp_path, mutation)
    with pytest.raises(ValueError, match="nine expected unique case IDs and stage kinds"):
        await getattr(companion, mode)(ROOT, evidence)


@pytest.mark.parametrize("python_options", [[], ["-O"]])
async def test_standalone_replay_cannot_report_success_for_an_empty_panel(tmp_path, python_options):
    evidence = altered_evidence(tmp_path, "empty")
    result = subprocess.run(
        [sys.executable, *python_options, str(SPEC.origin), "replay", "--root", str(ROOT), "--evidence", str(evidence)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert '"exact_comparison": true' not in result.stdout
    assert "nine expected unique case IDs and stage kinds" in result.stderr


@pytest.mark.parametrize("python_options", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize("mutation", ["output", "request", "fixture-hash", "source-hash"])
async def test_standalone_replay_validates_complete_panels_with_optimization(tmp_path, python_options, mutation):
    evidence = altered_evidence(tmp_path, "reordered")
    cases = json.loads((evidence / "cases.json").read_text())
    case = next(case for case in cases if case["id"] == "generator-primary-success")
    if mutation == "output":
        case["expected"]["answer"] = "DELIBERATELY ALTERED"
    elif mutation == "request":
        case["calls"][0]["payload"]["model"] = "DELIBERATELY ALTERED"
    companion.save(evidence / "cases.json", cases)
    manifest = json.loads((evidence / "manifest.json").read_text())
    manifest["files"]["cases.json"] = companion.sha(evidence / "cases.json")
    if mutation == "fixture-hash":
        manifest["files"]["cases.json"] = "0" * 64
    elif mutation == "source-hash":
        manifest["source_hashes"] = {"apps/api/src/assistant_rh_api/core/pipeline/steps/generator.py": "0" * 64}
    companion.save(evidence / "manifest.json", manifest)
    options = ["--verify-source-hashes"] if mutation == "source-hash" else []
    result = subprocess.run(
        [sys.executable, *python_options, str(SPEC.origin), "replay", "--root", str(ROOT), "--evidence", str(evidence), *options],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert '"exact_comparison": true' not in result.stdout
    expected_error = {
        "output": "output mismatch",
        "request": "exact request mismatch",
        "fixture-hash": "fixture hash mismatch",
        "source-hash": "source changed",
    }[mutation]
    assert expected_error in result.stderr


@pytest.mark.parametrize("python_options", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize("mode", ["replay", "check"])
async def test_standalone_valid_replay_and_controls_work_with_optimization(python_options, mode):
    result = subprocess.run(
        [sys.executable, *python_options, str(SPEC.origin), mode, "--root", str(ROOT), "--evidence", str(EVIDENCE / "synthetic-evidence")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if mode == "replay":
        assert report["exact_comparison"] and len(report["cases"]) == 9
    else:
        assert report["baseline_passed"] and len(report["controls"]) == 5
        assert all(control["detected"] for control in report["controls"])


async def test_complete_reordered_panel_keeps_negative_controls_meaningful(tmp_path):
    evidence = altered_evidence(tmp_path, "reordered")
    result = await companion.check(ROOT, evidence)
    assert result["baseline_passed"] and len(result["controls"]) == 5


async def test_record_modified_sources_declares_an_unverified_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    for path in ("apps/api/src", "packages/rag-pipeline/src", "packages/shared-config/src", "packages/data-engineering/src"):
        shutil.copytree(ROOT / path, snapshot / path, ignore=shutil.ignore_patterns("__pycache__"))
    relative = "apps/api/src/assistant_rh_api/core/pipeline/steps/generator.py"
    modified = snapshot / relative
    modified.write_text(modified.read_text() + "\n# Synthetic provenance regression check.\n")
    output = tmp_path / "recorded"
    result = subprocess.run(
        [sys.executable, str(SPEC.origin), "record", "--root", str(snapshot), "--evidence", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["head"] is None
    assert manifest["source_revision_status"] == "unverified_snapshot"
    assert manifest["source_hashes"][relative] == companion.sha(modified)
    assert manifest["source_hashes"][relative] != companion.sha(ROOT / relative)


async def test_synthetic_negative_controls_detect_semantic_changes():
    result = await companion.check(ROOT, EVIDENCE / "synthetic-evidence")
    assert result["baseline_passed"]
    assert len(result["controls"]) == 5
    assert all(control["detected"] for control in result["controls"])


async def test_recorded_grounding_audit_covers_every_answer_block_and_exact_sources():
    audit = json.loads((EVIDENCE / "grounding-audit.json").read_text())
    archive = RECORDED / "c5-parity-companion.tar.gz"
    hashes = dict(line.split(maxsplit=1)[::-1] for line in (RECORDED / "SHA256SUMS").read_text().splitlines())
    assert companion.sha(archive) == hashes[archive.name]
    with tarfile.open(archive) as bundle:
        recordings = {
            Path(member.name).stem: bundle.extractfile(member).read()
            for member in bundle.getmembers()
            if member.isfile() and member.name.startswith("c5-parity-companion/evidence/rag-") and member.name.endswith(".json")
        }
    assert {case["id"] for case in audit["cases"]} == set(recordings)
    for case in audit["cases"]:
        raw = recordings[case["id"]]
        assert hashlib.sha256(raw).hexdigest() == case["recording_sha256"]
        recorded = json.loads(raw)
        answer = recorded["generator_expected"]["answer"]
        assert hashlib.sha256(answer.encode()).hexdigest() == case["answer_sha256"]
        assert case["ministry"] == recorded["ministry"]
        blocks = [block for block in re.split(r"\n\n|\n(?=- )", answer) if block.strip()]
        reviewed = case["units"] + case["ignored_headings"]
        assert sorted(unit["block_index"] for unit in reviewed) == list(range(len(blocks)))
        for unit in reviewed:
            assert unit["answer_text"] == blocks[unit["block_index"]]
            assert answer[unit["answer_start"] : unit["answer_end"]] == unit["answer_text"]
        for heading in case["ignored_headings"]:
            assert heading["answer_text"].startswith("### ")
            assert "article" not in heading["answer_text"].lower()
        for unit in case["units"]:
            assert unit["support"]
            for evidence in unit["support"]:
                source = recorded["generator_input"][evidence["source_index"]]["content"]
                assert hashlib.sha256(source.encode()).hexdigest() == evidence["source_sha256"]
                assert source[evidence["source_start"] : evidence["source_end"]] == evidence["quote"]
                assert evidence["quote"] in recorded["generator_call"]["messages"][-1]["content"]
