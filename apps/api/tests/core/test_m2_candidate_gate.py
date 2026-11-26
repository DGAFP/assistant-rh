"""A new capture keeps the published M1 coverage, bindings and tolerances."""

import json

import pytest

from scripts.conformance import m2_gate
from scripts.conformance.m0b_values import ROOT, digest


@pytest.fixture
def candidate(tmp_path):
    proof = m2_gate.read_json(m2_gate.FOLLOWUP)
    previous = m2_gate.read_json(ROOT / "docs/evals/evidence/m2_m1_gate_same_capture_20261125.json")
    proof["capture"].update(campaign="new_candidate", sha256="1" * 64)
    proof["retrieval_recall"]["archive_sha256"] = "1" * 64
    proof["exact_http_replay"]["sha256"] = "1" * 64
    proof["deployment"]["git_sha"] = "2" * 40
    path = tmp_path / "candidate-proof.json"
    path.write_text(json.dumps(proof))
    inputs = tmp_path / "judge-inputs.json"
    inputs.write_text(json.dumps([{"question_id": question_id} for question_id in previous["manifest"]["eval_scope"]["question_ids"]]))
    manifest = previous["manifest"] | m2_gate.capture_binding(proof) | {"input_sha256": digest(inputs)}
    (tmp_path / "judge-manifest.json").write_text(json.dumps(manifest))
    summary = previous["judgment"] | {"manifest": manifest}
    (tmp_path / "judge-summary.json").write_text(json.dumps(summary))
    return tmp_path, path, summary


def test_report_uses_candidate_evidence_and_still_checks_input_integrity(candidate):
    directory, proof, _ = candidate
    result = m2_gate.report(directory, proof)
    assert result["gate"] == "pass" and result["deployment"]["git_sha"] == "2" * 40
    (directory / "judge-inputs.json").write_text('["changed"]')
    with pytest.raises(ValueError, match="registered inputs"):
        m2_gate.report(directory, proof)


def test_candidate_does_not_pass_with_a_missing_judgment(candidate):
    directory, proof, summary = candidate
    summary["outcomes"]["pass"] -= 1
    (directory / "judge-summary.json").write_text(json.dumps(summary))
    result = m2_gate.report(directory, proof)
    assert result["gate"] == "fail" and not result["complete"]


def test_self_consistent_hashes_cannot_hide_an_empty_question_inventory(candidate):
    directory, proof, summary = candidate
    inputs = directory / "judge-inputs.json"
    inputs.write_text("[]")
    manifest = summary["manifest"] | {"input_sha256": digest(inputs)}
    (directory / "judge-manifest.json").write_text(json.dumps(manifest))
    (directory / "judge-summary.json").write_text(json.dumps(summary | {"manifest": manifest}))
    with pytest.raises(ValueError, match="complete M1 question inventory"):
        m2_gate.report(directory, proof)
