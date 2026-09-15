"""Replay published synthetic branches and validate grounding audit provenance.

Grounding judgments are a manual review. These tests check exact quotations and
complete answer coverage, not semantic entailment or universal answer quality.
"""

import hashlib
import importlib.util
import json
import re
import socket
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
