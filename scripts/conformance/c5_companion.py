"""Record C5 calls on frozen C4 inputs, then replay the API core without network.

This is a new companion, never a reconstruction of the original M0b calls.
Record reads staging prompts and calls inference; replay needs only the stdlib
and this checkout's API core. Outputs contain the public panel/source text and
full provider reply text, never credentials or connection parameters.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def dump(path, value):
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sections(values, models):
    return tuple(
        models.AggregatedSection(
            **{
                **row,
                "chunks": tuple(models.RetrievedChunk(**chunk) for chunk in row["chunks"]),
            }
        )
        for row in values
    )


def configured(cls, values, provider_enum):
    return cls(**{key: provider_enum(value) if key in ("provider", "fallback_provider") else value for key, value in values.items()})


def verified_c4_inputs(source):
    """Freeze exact archive-matching inputs before any external I/O."""
    archive = ROOT / "tests/conformance/companions/c4-staging-20260914/c4-parity-companion.tar.gz"
    inputs = {}
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            name = Path(member.name).name
            if "/evidence-v2/" not in member.name or not (name == "config.json" or name.startswith("rag-") and name.endswith(".json")):
                continue
            expected = bundle.extractfile(member).read()
            actual = (source / name).read_bytes()
            assert actual == expected, f"C4 input differs from accepted archive: {name}"
            inputs[name] = (json.loads(actual), hashlib.sha256(actual).hexdigest())
    assert len(inputs) == 5 and "config.json" in inputs, "C4 config and four RAG inputs required"
    assert {p.name for p in source.glob("rag-*.json")} == set(inputs) - {"config.json"}, "unexpected C4 case"
    return inputs


def record(output, source, env_file):
    import psycopg
    from dotenv import dotenv_values
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    inputs = verified_c4_inputs(source)
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    logging.basicConfig(filename=output / "private-record.log", level=logging.WARNING)
    values = dotenv_values(env_file)
    staging, production = values.get("SCW_POSTGRES_DSN_STAGING"), values.get("SCW_POSTGRES_DSN_PROD")
    assert staging and production, "explicit staging and production identities required"
    target, other = conninfo_to_dict(staging), conninfo_to_dict(production)
    assert tuple(target.get(k) for k in ("host", "port", "dbname")) != tuple(other.get(k) for k in ("host", "port", "dbname"))
    # Prevent ambient libpq service/options from overriding the explicit target.
    for key in list(os.environ):
        if key.startswith("PG"):
            del os.environ[key]
    for key in ("ALBERT_API_KEY", "ALBERT_BASE_URL", "SCALEWAY_API_KEY", "SCALEWAY_BASE_URL"):
        if values.get(key):
            os.environ[key] = values[key]
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    dsn = make_conninfo(
        staging,
        sslmode="require",
        connect_timeout="10",
        options="-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=3000",
    )
    config = inputs["config.json"][0]
    # Configuration is fixed to the accepted C4 recording. Read only these two
    # prompt names from staging; no pipeline/config write or corpus query occurs.
    prompt_names = [config["selector"]["prompt_name"], config["generation"]["system_prompt_name"]]
    snapshots = {}
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT current_setting('transaction_read_only'), current_setting('default_transaction_read_only')").fetchone()
        assert row == ("on", "on"), "read-only session required"
        for name in prompt_names:
            row = conn.execute("SELECT content FROM system_prompts WHERE name = %s AND is_active = TRUE", (name,)).fetchone()
            assert row and row[0], "configured prompt must exist for this real recording"
            content = row[0]
            snapshots[name] = {"name": name, "content": content, "revision": hashlib.sha256(content.encode()).hexdigest(), "origin": "database"}
    # No DB connection is used below; the retained prompt loader is replaced
    # with the captured raw prompt plus a single explicit request date.
    from assistant_rh_rag_pipeline import config as old_config
    from assistant_rh_rag_pipeline import context_selector, generator, llm_client, models
    from assistant_rh_rag_pipeline.ministry_scope import resolve_ministry

    today = datetime.now().strftime("%Y-%m-%d")
    active_calls = []

    def chat(client, prompt, system_prompt=None):
        messages = client._build_messages(prompt, system_prompt)
        response = client._client.chat.completions.create(
            model=client.model,
            messages=messages,
            temperature=client.temperature,
            timeout=client.timeout,
        )
        text = (response.choices[0].message.content or "").strip()
        active_calls.append(
            {
                "provider": client.provider,
                "model": client.model,
                "temperature": client.temperature,
                "messages": messages,
                "response": response.model_dump(mode="json"),
                "text": text,
            }
        )
        return text

    def prompt(name, fallback_name, *, default=None):
        return snapshots[name]["content"].replace("{today}", today)

    fixture_paths = []
    with (
        patch.object(llm_client.LLMClient, "chat", chat),
        patch.object(context_selector, "load_prompt", prompt),
        patch.object(generator, "load_prompt", prompt),
    ):
        for name in sorted(set(inputs) - {"config.json"}):
            captured, source_hash = inputs[name]
            fixture = captured["fixture"]
            raw_sections = captured["aggregation_calls"][-1]["expected"]["sections"]
            query = captured["aggregation_calls"][-1]["query"]
            ministry = fixture.get("ministry")
            selector = context_selector.ContextSelector(configured(old_config.SelectorConfig, config["selector"], old_config.LLMProvider))
            active_calls.clear()
            selected = selector.select(query, list(sections(raw_sections, models)), resolve_ministry(ministry))
            assert len(active_calls) == 1, "real selector recording requires exactly one successful call"
            selection_call = active_calls[0]
            selection_expected = {
                "sections": plain(selected),
                "decisions": selector.last_decisions,
                "raw_response": selector.last_raw_response,
                "reason": selector.last_reasoning,
                "all_rejected": selector.all_rejected,
                "prompt_chars": selector.last_prompt_chars,
            }
            gen = generator.StreamingGenerator(configured(old_config.GenerationConfig, config["generation"], old_config.LLMProvider))
            active_calls.clear()
            # Independent generation-stage input from C4's frozen final context;
            # this is deliberately not an assembled pipeline replay.
            answer = gen.generate(query, [models.ContextItem(**row) for row in captured["final_context"]], resolve_ministry(ministry))
            assert len(active_calls) == 1, "recording requires one successful completion"
            assert gen.provider_used == config["generation"]["provider"] and gen.fallback_count == 0, "primary success required"
            result = {
                "id": fixture["id"],
                "query": query,
                "ministry": ministry,
                "today": today,
                "source_c4_sha256": source_hash,
                "selector_input": raw_sections,
                "selector_call": selection_call,
                "selector_expected": selection_expected,
                "generator_input": captured["final_context"],
                "generator_call": active_calls[0],
                "generator_expected": {
                    "answer": answer,
                    "system_prompt": gen.last_system_prompt,
                    "user_prompt": gen.last_full_prompt,
                    "provider": gen.provider_used,
                    "fallback_count": gen.fallback_count,
                },
            }
            target_path = output / name
            dump(target_path, result)
            fixture_paths.append(target_path)
            print(
                f"Recorded {fixture['id']}: {len(raw_sections)} selector candidates, {len(selected)} selected, {len(answer)} answer characters",
                flush=True,
            )
    dump(output / "config.json", config)
    dump(output / "prompts.json", snapshots)
    source_files = sorted((ROOT / "apps/api/src/assistant_rh_api/core").rglob("*.py"))
    source_files += [
        ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline" / name
        for name in (
            "context_selector.py",
            "generator.py",
            "llm_client.py",
            "db_helpers.py",
            "ministry_scope.py",
            "models.py",
            "context_builder.py",
        )
    ]
    manifest = {
        "schema": "c5-recorded-companion-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Independent C5 stages on frozen C4 inputs; fresh staging prompts and live primary LLM replies. Not historical M0b or C6/M1.",
        "source": "tests/conformance/companions/c4-staging-20260914/c4-parity-companion.tar.gz",
        "source_archive_sha256": digest(ROOT / "tests/conformance/companions/c4-staging-20260914/c4-parity-companion.tar.gz"),
        "staging_read_only": True,
        "config_origin": "accepted C4 recording, not refreshed live config",
        "files": {p.name: digest(p) for p in fixture_paths + [output / "config.json", output / "prompts.json"]},
        "source_hashes": {str(p.relative_to(ROOT)): digest(p) for p in source_files},
    }
    dump(output / "manifest.json", manifest)


async def replay(output):
    from assistant_rh_api.core.models import context, retrieval
    from assistant_rh_api.core.models.configuration import Prompt, Snapshot
    from assistant_rh_api.core.models.inference import Attempt, Completion, TokenUsage
    from assistant_rh_api.core.models.rag_configuration import GenerationConfig, LLMProvider, SelectorConfig
    from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
    from assistant_rh_api.core.pipeline.steps.generator import Generator

    manifest = json.loads((output / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert digest(output / name) == expected, f"fixture hash mismatch: {name}"
    config = json.loads((output / "config.json").read_text())
    prompts = json.loads((output / "prompts.json").read_text())

    class Store:
        async def get(self, name):
            raw = prompts.get(name)
            return Snapshot(Prompt(name, raw["content"]), raw["revision"], raw["origin"]) if raw else None

    class RecordedLLM:
        def __init__(self, recorded):
            self.recorded = recorded
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            assert plain(request.messages) == self.recorded["messages"], "exact LLM request mismatch"
            assert request.temperature == self.recorded["temperature"], "temperature mismatch"
            response = self.recorded["response"]
            usage = response.get("usage")
            usage = TokenUsage(*(usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens"))) if usage else None
            return Completion(
                self.recorded["text"],
                self.recorded["provider"],
                self.recorded["model"],
                (Attempt(self.recorded["provider"], self.recorded["model"]),),
                response["choices"][0]["finish_reason"],
                usage,
            )

    report = []
    for path in sorted(output.glob("rag-*.json")):
        case = json.loads(path.read_text())
        candidates = tuple(
            context.AggregatedSection(**{**row, "chunks": tuple(retrieval.RetrievedChunk(**c) for c in row["chunks"])})
            for row in case["selector_input"]
        )
        llm = RecordedLLM(case["selector_call"])
        selector = ContextSelector(configured(SelectorConfig, config["selector"], LLMProvider), Store(), Store(), llm)
        selection = await selector.select(case["query"], candidates, case["ministry"], today=case["today"])
        actual = {
            "sections": plain(selection.sections),
            "decisions": plain(selection.diagnostics.decisions),
            "raw_response": selection.diagnostics.raw_response,
            "reason": selection.diagnostics.reason,
            "all_rejected": selection.all_rejected,
            "prompt_chars": selection.diagnostics.prompt_chars,
        }
        assert actual == case["selector_expected"], f"selector differs: {case['id']}"
        assert llm.calls == 1
        llm = RecordedLLM(case["generator_call"])
        generator = Generator(configured(GenerationConfig, config["generation"], LLMProvider), Store(), Store(), llm)
        result = await generator.generate(
            case["query"], tuple(context.ContextItem(**row) for row in case["generator_input"]), case["ministry"], today=case["today"]
        )
        actual = {
            "answer": result.answer,
            "system_prompt": result.diagnostics.request.messages[0].content,
            "user_prompt": result.diagnostics.request.messages[-1].content,
            "provider": result.diagnostics.outcome.provider,
            "fallback_count": result.diagnostics.fallback_count,
        }
        assert actual == case["generator_expected"], f"generator differs: {case['id']}"
        assert llm.calls == 1
        report.append(
            {
                "id": case["id"],
                "selector_candidates": len(candidates),
                "selected": len(selection.sections),
                "selection_status": selection.diagnostics.status,
                "context_items": len(case["generator_input"]),
                "answer_chars": len(result.answer),
                "generator_provider": result.diagnostics.outcome.provider,
                "generator_model": result.diagnostics.outcome.model,
                "generator_fallback_count": result.diagnostics.fallback_count,
                "generator_usage": plain(result.diagnostics.outcome.usage),
                "exact": True,
            }
        )
    assert len(report) == 4, "four RAG cases required"
    return {
        "exact_comparison": True,
        "cases": report,
        "scope": manifest["scope"],
        "candidate_source_hashes": {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / "apps/api/src/assistant_rh_api/core").rglob("*.py"))},
    }


async def negative_controls(output):
    """Prove that a green live replay becomes red on three independent mutations."""
    await replay(output)
    results = []
    for mutation in ("unhashed-fixture", "prompt-with-new-hash", "answer-with-new-hash"):
        with tempfile.TemporaryDirectory(prefix="c5-negative-") as directory:
            copied = Path(directory) / "evidence"
            shutil.copytree(output, copied)
            if mutation == "prompt-with-new-hash":
                path = copied / "prompts.json"
                data = json.loads(path.read_text())
                data[next(iter(data))]["content"] += " CHANGED POLICY"
            else:
                path = next(copied.glob("rag-*.json"))
                data = json.loads(path.read_text())
                data["generator_expected"]["answer"] += " UNSUPPORTED CLAIM"
            dump(path, data)
            if mutation != "unhashed-fixture":
                manifest = json.loads((copied / "manifest.json").read_text())
                manifest["files"][path.name] = digest(path)
                dump(copied / "manifest.json", manifest)
            try:
                await replay(copied)
            except AssertionError as exc:
                results.append({"mutation": mutation, "detected": True, "reason": str(exc)})
            else:
                raise AssertionError(f"mutation was not detected: {mutation}")
    return {"baseline_passed": True, "negative_controls": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "replay", "check"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, help="extracted C4 evidence-v2 directory (record only)")
    parser.add_argument("--env-file", type=Path, help="explicit private dotenv file (record only)")
    args = parser.parse_args()
    if args.mode == "record":
        if not args.source or not args.env_file:
            parser.error("record requires --source and --env-file")
        record(args.output, args.source, args.env_file)
    else:
        with (
            patch.object(socket.socket, "connect", side_effect=AssertionError("offline replay forbids network")),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("offline replay forbids network"),
            ),
        ):
            action = replay if args.mode == "replay" else negative_controls
            print(json.dumps(asyncio.run(action(args.output)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
