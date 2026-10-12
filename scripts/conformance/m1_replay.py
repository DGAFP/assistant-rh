"""Replay the M1 panel after the retrieval boundary, with network denied.

Private fixtures use the M0b tape contract plus a shared, rehydrated chunk pool.
This does not replay SQL searches, embedding vectors or provider transports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.conformance.m0b_replay import deny_network, replay_case
from scripts.conformance.m0b_values import ROOT, Tape, assert_equal, digest, dump, invocation, require


@contextmanager
def frozen_retrieval(case):
    from assistant_rh_api import bootstrap
    from assistant_rh_api.core.models.retrieval import RetrievedChunk
    from assistant_rh_api.core.pipeline.steps.retrieval import RetrievalResult
    from assistant_rh_rag_pipeline.models import RetrievedChunk as LegacyChunk
    from assistant_rh_rag_pipeline.retriever import Retriever

    calls = {"legacy": 0, "core": 0}

    def take(runtime, query):
        calls[runtime] += 1
        require(calls[runtime] == 1, "Unexpected repeated retrieval")
        assert_equal(query, case["retrieval_query"], "frozen retrieval query")
        return case["retrieved_chunks"]

    def legacy(instance, query, **kwargs):
        return [LegacyChunk(**row) for row in take("legacy", query)]

    class Core:
        def __init__(self, **kwargs):
            pass

        async def retrieve(self, query, config, *, selected_ministry, search_mode, top_k):
            assert_equal(selected_ministry, case["effective_ministry"], "retrieval ministry")
            assert_equal(top_k, case["config"]["retrieval"]["initial_top_k"], "retrieval top_k")
            assert_equal(search_mode.value, case["config"]["retrieval"]["search_mode"], "retrieval mode")
            return RetrievalResult(tuple(RetrievedChunk(**row) for row in take("core", query)))

    with patch.object(Retriever, "retrieve", legacy), patch.object(bootstrap, "Retriever", Core):
        yield calls


def replay_legacy(case):
    """Run the historical engine again, using only the recorded dependency tape."""
    from assistant_rh_rag_pipeline import context_builder, db_helpers, query_processor, reranker, section_aggregator
    from assistant_rh_rag_pipeline.admin import RuntimeRAGConfig, runtime_config_to_rag_config
    from openai.resources.chat.completions import Completions

    from scripts.conformance import m0b_record

    # Detach mutable legacy references from the reference recording.
    case = json.loads(json.dumps(case))
    inference = Tape(case["inference"])
    content = Tape([{**row, "response": row["evidence"]} for row in case["stores"] if row["operation"].startswith("content.")])
    settings = Tape([row for row in case["stores"] if row["operation"].startswith(("prompts.", "acronyms.", "packaged."))])
    models = {row["request"]["model"]: row["request"]["provider"] for row in case["inference"] if row["operation"] == "llm.complete"}

    def create(client, **kwargs):
        response = inference.take(
            "llm.complete",
            {"provider": models[kwargs["model"]], "model": kwargs["model"], "temperature": kwargs["temperature"], "messages": kwargs["messages"]},
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response["text"]))],
            model_dump=lambda **unused: response["response"],
        )

    def rank(client, query, texts, top_k=None):
        return inference.take("reranker.rerank", {"query": query, "documents": list(texts), "top_k": top_k})["result"]

    def read(operation, *args):
        return content.take(operation, invocation(args, {}, operation))

    def references(builder, numbers):
        if not numbers:
            return {}
        rows = read("content.references", tuple(numbers))["rows"]
        return {r["number"]: {"cid": r["cid"] or "", "url": r["url"] or "", "title": r["full_title"] or ""} for r in rows if r.get("cid")}

    def prompt(name, *, render_today=True):
        value = settings.take("prompts.get", invocation((name,), {}))
        if value is None:
            return None
        text = value["value"]["content"]
        return text.replace("{today}", case["today"]) if render_today else text

    def acronyms():
        value = settings.take("acronyms.load", invocation((), {}))
        return {row["short"]: row["expansion"] for row in value["value"]}

    config = runtime_config_to_rag_config(RuntimeRAGConfig.from_dict(case["runtime_config"]))
    assert_equal(config.to_dict(), case["config"], "legacy configuration")
    actual = {**case, "inference": [], "stores": [], "expected": {"stages": []}}
    with (
        deny_network(),
        patch.object(Completions, "create", create),
        patch.object(reranker.AlbertReranker, "rerank", rank),
        patch.object(section_aggregator.SectionAggregator, "_fetch_sections", lambda owner, ids: read("content.sections", tuple(ids))["rows"]),
        patch.object(context_builder.ContextBuilder, "_load_full_document", lambda owner, doc: read("content.documents", (doc,))["row"]),
        patch.object(context_builder.ContextBuilder, "_resolve_cids", references),
        patch.object(db_helpers, "get_prompt_content", prompt),
        patch.object(query_processor, "get_acronym_dict", acronyms),
        patch.object(m0b_record, "attach_content", lambda *args: None),
        patch.dict("os.environ", {"ALBERT_API_KEY": "offline-fixture", "SCALEWAY_API_KEY": "offline-fixture", "PYTHON_DOTENV_DISABLED": "1"}),
        frozen_retrieval(actual) as calls,
    ):
        m0b_record.record_legacy(actual, "postgresql://offline.invalid/unused", config)
    inference.assert_consumed()
    content.assert_consumed()
    settings.assert_consumed()
    assert_equal(actual["expected"], case["expected"], "legacy recorded stages and result")
    assert_equal(actual["inference"], case["inference"], "legacy inference requests")
    expected = int(any(s["stage"] == "retriever" for s in case["expected"]["stages"]))
    assert_equal(calls["legacy"], expected, "legacy retrieval consumption")


async def replay_frozen_case(case):
    import socket

    import psycopg

    def denied(*args, **kwargs):
        raise AssertionError("Network and database access are forbidden during replay")

    with (
        deny_network(),
        patch.object(socket, "getaddrinfo", denied),
        patch.object(psycopg, "connect", denied),
        patch.object(psycopg.AsyncConnection, "connect", denied),
    ):
        replay_legacy(case)
        with frozen_retrieval(case) as calls:
            result = await replay_case(case)
    expected = int(any(s["stage"] == "retriever" for s in case["expected"]["stages"]))
    assert_equal(calls["core"], expected, "retrieval consumption")
    return result


async def negative_controls(case):
    results = []
    for mutation in ("provider-prompt", "retrieval-text", "context", "answer", "extra-call"):
        changed = json.loads(json.dumps(case))
        if mutation == "provider-prompt":
            call = next(row for row in changed["inference"] if row["operation"] == "llm.complete")
            call["request"]["messages"][-1]["content"] += " CHANGED"
        elif mutation == "retrieval-text":
            changed["retrieved_chunks"][0]["text"] += " CHANGED"
        elif mutation == "context":
            stage = next(row for row in changed["expected"]["stages"] if row["stage"] == "context-builder")
            stage["output"]["formatted_context"] += " CHANGED"
        elif mutation == "answer":
            changed["expected"]["result"]["answer"] += " CHANGED"
        else:
            changed["inference"].append(changed["inference"][0])
        try:
            await replay_frozen_case(changed)
        except AssertionError:
            results.append({"mutation": mutation, "detected": True})
        else:
            raise AssertionError("Mutation was not detected: " + mutation)
    return results


async def replay(directory: Path, *, check=False):
    manifest = json.loads((directory / "manifest.json").read_text())
    require(manifest["schema"] == "m1-frozen-after-retrieval-v1", "Unsupported replay schema")
    ids = manifest["question_ids"]
    require(len(ids) == len(set(ids)) == 98, "Exactly 98 distinct questions required")
    require(set(manifest["files"]) == {f"q{qid}.json" for qid in ids}, "Incomplete fixture inventory")
    results = []
    control_case = None
    for qid in ids:
        path = directory / f"q{qid}.json"
        require(not path.is_symlink() and path.resolve().parent == directory.resolve(), "Unsafe fixture path")
        require(digest(path) == manifest["files"][path.name], "Fixture hash mismatch")
        case = json.loads(path.read_text())
        assert_equal(case["fixture"]["id"], f"q{qid}", "fixture identity")
        results.append(await replay_frozen_case(case))
        if control_case is None and case["retrieved_chunks"]:
            control_case = case
    return {
        "schema": manifest["schema"],
        "exact_comparison": True,
        "network": "denied",
        "scope": manifest["scope"],
        "manifest_sha256": digest(directory / "manifest.json"),
        "cases": results,
        "negative_controls": await negative_controls(control_case) if check else [],
        "source_hashes": {
            str(path.relative_to(ROOT)): digest(path)
            for folder in ("apps/api/src/assistant_rh_api", "packages/rag-pipeline/src", "scripts/conformance")
            for path in sorted((ROOT / folder).rglob("*.py"))
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Also require five intentional mutations to fail")
    args = parser.parse_args()
    result = asyncio.run(replay(args.directory, check=args.check))
    dump(args.report, result)
    print(json.dumps({"exact_comparison": True, "questions": len(result["cases"]), "network": result["network"]}))


if __name__ == "__main__":
    main()
