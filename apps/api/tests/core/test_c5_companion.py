"""Hermetic recorder/replay checks. Provider and staging I/O are simulated."""

import importlib.util
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from assistant_rh_rag_pipeline import llm_client

ROOT = Path(__file__).resolve().parents[4]
SELECTOR_TRANSITION = ROOT / "tests/conformance/companions/selector-markdown-wrapper-20261021/transition.json"
SPEC = importlib.util.spec_from_file_location("c5_companion", ROOT / "scripts/conformance/c5_companion.py")
companion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(companion)
pytestmark = pytest.mark.anyio


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    import dotenv
    import psycopg

    source = tmp_path / "c4"
    source.mkdir()
    archive = ROOT / "tests/conformance/companions/c4-staging-20260914/c4-parity-companion.tar.gz"
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            name = Path(member.name).name
            if "/evidence-v2/" in member.name and (name == "config.json" or name.startswith("rag-") and name.endswith(".json")):
                (source / name).write_bytes(bundle.extractfile(member).read())
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, parameters=()):
            queries.append(sql)
            assert sql.startswith("SELECT ")
            if "current_setting" in sql:
                value = ("on", "on")
            else:
                name = "selector.md" if parameters[0].startswith("v3_selector") else parameters[0]
                value = ((ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts" / name).read_text(),)
            return SimpleNamespace(fetchone=lambda: value)

    def connect(dsn):
        assert "default_transaction_read_only=on" in dsn and "sslmode=require" in dsn
        return Connection()

    def create(**request):
        text = '{"selected_ids": [0, 2], "reason": "synthetic recorded reply"}' if request["model"] == "openweight-large" else "Synthetic answer."
        data = {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        }
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], model_dump=lambda **kwargs: data)

    def client_init(client, provider, model, temperature=0.0, system_prompt=None, **kwargs):
        client.provider, client.model, client.temperature = provider, model, temperature
        client.system_prompt, client.timeout = system_prompt, 1
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(
        dotenv,
        "dotenv_values",
        lambda path: {
            "SCW_POSTGRES_DSN_STAGING": "host=staging.test dbname=staging",
            "SCW_POSTGRES_DSN_PROD": "host=production.test dbname=production",
        },
    )
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(llm_client.LLMClient, "__init__", client_init)
    output = tmp_path / "recorded"
    companion.record(output, source, tmp_path / "not-a-real-env")
    assert len(queries) == 3
    return output


async def test_recorder_round_trip_matches_retained_stages(recorded, monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("offline replay must not use network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    result = await companion.replay(recorded)
    assert result["exact_comparison"] is True
    assert len(result["cases"]) == 4
    assert all(case["exact"] for case in result["cases"])


@pytest.mark.parametrize("mutation", ["unhashed-fixture", "prompt-with-new-hash", "expected-with-new-hash"])
async def test_replay_negative_controls(recorded, mutation):
    if mutation == "prompt-with-new-hash":
        path = recorded / "prompts.json"
        data = json.loads(path.read_text())
        data[next(iter(data))]["content"] += " CHANGED POLICY"
    else:
        path = next(recorded.glob("rag-*.json"))
        data = json.loads(path.read_text())
        data["selector_expected"]["sections"] = []
    companion.dump(path, data)
    if mutation != "unhashed-fixture":
        manifest = json.loads((recorded / "manifest.json").read_text())
        manifest["files"][path.name] = companion.digest(path)
        companion.dump(recorded / "manifest.json", manifest)
    with pytest.raises(AssertionError):
        await companion.replay(recorded)


async def test_c4_provenance_checks_run_before_database_or_provider_io(tmp_path, monkeypatch):
    import psycopg

    source = tmp_path / "changed-source"
    source.mkdir()
    archive = ROOT / "tests/conformance/companions/c4-staging-20260914/c4-parity-companion.tar.gz"
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            name = Path(member.name).name
            if "/evidence-v2/" in member.name and (name == "config.json" or name.startswith("rag-") and name.endswith(".json")):
                (source / name).write_bytes(bundle.extractfile(member).read())
    config = json.loads((source / "config.json").read_text())
    config["selector"]["model"] = "changed-model"
    companion.dump(source / "config.json", config)

    def fail_io(*args, **kwargs):
        pytest.fail("provenance must be checked before I/O")

    monkeypatch.setattr(psycopg, "connect", fail_io)
    monkeypatch.setattr(llm_client.LLMClient, "__init__", fail_io)
    with pytest.raises(AssertionError, match="C4 input differs"):
        companion.record(tmp_path / "must-not-be-created", source, tmp_path / "no-env")
    assert not (tmp_path / "must-not-be-created").exists()


@pytest.fixture
def published_c5(tmp_path):
    archive = ROOT / "tests/conformance/companions/c5-recorded-20260914/c5-parity-companion.tar.gz"
    checksums = archive.with_name("SHA256SUMS").read_text().splitlines()
    expected = next(line.split()[0] for line in checksums if line.split()[-1] == archive.name)
    assert companion.digest(archive) == expected
    evidence = tmp_path / "published-c5"
    evidence.mkdir()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if member.name.startswith("c5-parity-companion/evidence/") and member.name.endswith(".json"):
                (evidence / Path(member.name).name).write_bytes(bundle.extractfile(member).read())
    return evidence


async def test_published_live_companion_replays_versioned_selector_transition_without_network(published_c5, monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("published replay must remain offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    result = await companion.replay(published_c5, selector_transition=SELECTOR_TRANSITION)
    assert result["exact_comparison"] is True
    assert len(result["cases"]) == 4
    assert sum(case["selector_candidates"] for case in result["cases"]) == 80
    assert result["intentional_selector_changes"] == ["rag-acronym-contract"]
    assert sum(case["historical_selector_exact"] for case in result["cases"]) == 3


async def test_published_historical_reference_stays_strict_without_explicit_transition(published_c5):
    with pytest.raises(AssertionError, match="selector differs: rag-acronym-contract"):
        await companion.replay(published_c5)


@pytest.mark.parametrize("mutation", ["manifest", "extra-case", "expected-sections"])
async def test_selector_transition_rejects_wrong_reference_or_expectations(published_c5, tmp_path, mutation):
    data = json.loads(SELECTOR_TRANSITION.read_text())
    if mutation == "manifest":
        data["historical_manifest_sha256"] = "0" * 64
    elif mutation == "extra-case":
        data["cases"]["unknown-case"] = data["cases"]["rag-acronym-contract"]
    else:
        data["cases"]["rag-acronym-contract"]["expected"]["sections"] = []
    transition = tmp_path / "transition.json"
    companion.dump(transition, data)
    with pytest.raises(AssertionError):
        await companion.replay(published_c5, selector_transition=transition)


@pytest.mark.parametrize("mutation,message", [("prompt", "exact LLM request mismatch"), ("answer", "generator differs")])
async def test_transition_semantic_guards_reject_rehashed_inputs(published_c5, tmp_path, mutation, message):
    # First prove this fixture/reference pair is green, then deliberately move
    # its integrity pins to exercise the deeper semantic checks independently.
    await companion.replay(published_c5, selector_transition=SELECTOR_TRANSITION)
    if mutation == "prompt":
        path = published_c5 / "prompts.json"
        data = json.loads(path.read_text())
        config = json.loads((published_c5 / "config.json").read_text())
        data[config["selector"]["prompt_name"]]["content"] += " CHANGED POLICY"
    else:
        path = published_c5 / "rag-acronym-contract.json"
        data = json.loads(path.read_text())
        data["generator_expected"]["answer"] += " UNSUPPORTED CLAIM"
    companion.dump(path, data)
    manifest = json.loads((published_c5 / "manifest.json").read_text())
    manifest["files"][path.name] = companion.digest(path)
    companion.dump(published_c5 / "manifest.json", manifest)
    transition = json.loads(SELECTOR_TRANSITION.read_text())
    transition["historical_manifest_sha256"] = companion.digest(published_c5 / "manifest.json")
    if mutation == "answer":
        transition["cases"]["rag-acronym-contract"]["fixture_sha256"] = companion.digest(path)
    reference = tmp_path / "deliberately-rehashed-transition.json"
    companion.dump(reference, transition)
    with pytest.raises(AssertionError, match=message):
        await companion.replay(published_c5, selector_transition=reference)


@pytest.mark.parametrize("mutation", ["unhashed-fixture", "prompt-with-new-hash", "answer-with-new-hash"])
async def test_published_live_companion_detects_mutations(published_c5, mutation):
    if mutation == "prompt-with-new-hash":
        path = published_c5 / "prompts.json"
        data = json.loads(path.read_text())
        data[next(iter(data))]["content"] += " CHANGED POLICY"
    else:
        path = next(published_c5.glob("rag-*.json"))
        data = json.loads(path.read_text())
        data["generator_expected"]["answer"] += " UNSUPPORTED CLAIM"
    companion.dump(path, data)
    if mutation != "unhashed-fixture":
        manifest = json.loads((published_c5 / "manifest.json").read_text())
        manifest["files"][path.name] = companion.digest(path)
        companion.dump(published_c5 / "manifest.json", manifest)
    with pytest.raises(AssertionError):
        await companion.replay(published_c5, selector_transition=SELECTOR_TRANSITION)
