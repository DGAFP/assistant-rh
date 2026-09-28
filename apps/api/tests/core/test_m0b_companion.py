"""Replay the published full-engine recording against this checkout, offline."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts.conformance import m0b_companion as companion
from scripts.conformance.m0b_values import ROOT, digest, dump

EVIDENCE = ROOT / "tests/conformance/companions/m0b-full-20260917"
ARCHIVE = Path(os.environ["M0B_PRIVATE_ARCHIVE"]) if os.environ.get("M0B_PRIVATE_ARCHIVE") else None
pytestmark = pytest.mark.anyio


@pytest.fixture
def published(tmp_path, monkeypatch):
    if ARCHIVE is None or not ARCHIVE.is_file():
        pytest.fail("M0B_PRIVATE_ARCHIVE must point to the privately downloaded conformance archive")
    def blocked(*args, **kwargs):
        raise AssertionError("M0b replay must remain offline")

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    hashes = {line.split()[1]: line.split()[0] for line in (EVIDENCE / "SHA256SUMS").read_text().splitlines()}
    assert digest(ARCHIVE) == hashes[ARCHIVE.name]
    directory = tmp_path / "evidence"
    directory.mkdir()
    with tarfile.open(ARCHIVE) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if member.isfile() and str(path.parent) == "m0b-full-companion/evidence" and path.suffix == ".json":
                (directory / path.name).write_bytes(bundle.extractfile(member).read())
    return directory


async def test_seven_published_scenarios_replay_through_production_composition(published):
    result = await companion.replay(published)
    assert result["exact_comparison"] and result["network"] == "denied"
    assert len(result["cases"]) == 7
    assert [row["stages"] for row in result["cases"]] == [6, 6, 6, 6, 1, 1, 1]
    assert sum(row["inference_calls"] for row in result["cases"]) == 23
    assert all(row["exact"] for row in result["cases"])


async def test_published_recording_keeps_its_exact_source_snapshot(published):
    manifest, _ = companion.verify(published)
    with tarfile.open(ARCHIVE) as bundle:
        for path, expected in manifest["source_hashes"].items():
            content = bundle.extractfile("m0b-full-companion/recording-source/" + path).read()
            assert hashlib.sha256(content).hexdigest() == expected


async def test_five_negative_controls_detect_changes_to_inputs_and_outputs(published):
    result = await companion.check(published)
    assert len(result["negative_controls"]) == 5
    assert all(row["detected"] for row in result["negative_controls"])


@pytest.mark.parametrize("mutation", ["missing", "extra", "partial"])
async def test_incomplete_inventory_is_rejected_before_pipeline_io(published, monkeypatch, mutation):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid evidence must fail before constructing the production composition")

    monkeypatch.setattr(companion, "replay_case", unexpected)
    path = published / "manifest.json"
    manifest = json.loads(path.read_text())
    if mutation == "missing":
        manifest["files"].pop(next(iter(manifest["files"])))
    elif mutation == "extra":
        manifest["files"]["extra.json"] = "0" * 64
    else:
        manifest["complete_panel"] = False
    dump(path, manifest)
    with pytest.raises(AssertionError, match="seven"):
        await companion.replay(published)


@pytest.mark.parametrize("options", [[], ["-O"], ["-OO"]])
@pytest.mark.parametrize("mutation", ["empty-panel", "fixture-hash", "answer"])
async def test_cli_rejects_invalid_evidence_even_with_python_optimization(published, options, mutation):
    manifest = json.loads((published / "manifest.json").read_text())
    if mutation == "empty-panel":
        manifest.update(files={}, query_count=0, complete_panel=False)
    else:
        path = published / "rag-acronym-contract.json"
        case = json.loads(path.read_text())
        case["expected"]["result"]["answer"] += " UNSUPPORTED CLAIM"
        dump(path, case)
        if mutation == "answer":
            manifest["files"][path.name] = digest(path)
    dump(published / "manifest.json", manifest)
    result = subprocess.run(
        [sys.executable, *options, "-m", "scripts.conformance.m0b_companion", "replay", str(published)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert '"exact_comparison": true' not in result.stdout
    message = {"empty-panel": "seven complete scenarios", "fixture-hash": "Fixture hash mismatch", "answer": "result: exact JSON differs"}
    assert message[mutation] in result.stderr


@pytest.mark.parametrize("options", [[], ["-O"], ["-OO"]])
async def test_valid_cli_replay_and_controls_work_with_python_optimization(published, options):
    result = subprocess.run(
        [sys.executable, *options, "-m", "scripts.conformance.m0b_companion", "check", str(published)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report["negative_controls"]) == 5 and all(row["detected"] for row in report["negative_controls"])
