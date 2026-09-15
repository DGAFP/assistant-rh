"""Offline C5 branch recordings from the retained runtime, replayed through the API gateway.

All provider responses and outages in this companion are SYNTHETIC.
No historical M0b, live provider, end-to-end pipeline or answer-quality claim.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def save(path, value):
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def section_values():
    return [
        dict(
            section_id=f"s{i}",
            heading=f"Section fictive {i}",
            markdown=f"Règle synthétique numéro {i}.",
            chunks=[],
            score=1 - i / 10,
            document_id=f"doc{i}",
            publisher="MATTE",
        )
        for i in range(8)
    ]


def item_values():
    return [
        dict(
            section_id="s0",
            heading="Règlement fictif",
            content="La durée prévue par ce règlement fictif est de trois jours.",
            score=1.0,
            publisher="MATTE",
            document_title="Règlement fictif",
        )
    ]


def specifications():
    base = dict(query="Quelle durée prévoit ce règlement fictif ?", ministry="matte", today="2026-09-15")
    reply = "Ce règlement fictif prévoit trois jours."
    return [
        dict(
            base,
            id="selector-selection-topup",
            kind="selector",
            sections=section_values(),
            floor=4,
            responses=[dict(provider="albert", status=200, text='{"selected_ids":[2,0,2],"reason":"Sections utiles"}')],
        ),
        dict(
            base,
            id="selector-total-rejection",
            kind="selector",
            sections=section_values(),
            floor=4,
            responses=[dict(provider="albert", status=200, text='{"selected_ids":[],"reason":"Aucune section pertinente"}')],
        ),
        dict(
            base,
            id="selector-parse-failure",
            kind="selector",
            sections=section_values(),
            floor=0,
            responses=[dict(provider="albert", status=200, text="synthetic malformed response")],
        ),
        dict(
            base,
            id="selector-provider-outage",
            kind="selector",
            sections=section_values(),
            floor=0,
            responses=[dict(provider="albert", status=503, text="")],
        ),
        dict(
            base, id="generator-primary-success", kind="generator", items=item_values(), responses=[dict(provider="albert", status=200, text=reply)]
        ),
        dict(
            base,
            id="generator-empty-context",
            kind="generator",
            items=[],
            responses=[dict(provider="albert", status=200, text="Les sources fournies ne permettent pas de répondre.")],
        ),
        dict(
            base,
            id="generator-fallback",
            kind="generator",
            items=item_values(),
            responses=[dict(provider="albert", status=503, text=""), dict(provider="scaleway", status=200, text=reply)],
        ),
        dict(
            base,
            id="generator-double-outage",
            kind="generator",
            items=item_values(),
            responses=[dict(provider="albert", status=503, text=""), dict(provider="scaleway", status=503, text="")],
        ),
        dict(base, id="final-rejection-no-answer", kind="no_answer", items=[], responses=[]),
    ]


def record(root, output):
    from assistant_rh_rag_pipeline import config, context_selector, generator, llm_client, models, pipeline
    from assistant_rh_rag_pipeline.ministry_scope import resolve_ministry

    assert not output.exists(), "never overwrite a recording"
    output.mkdir()
    prompts_dir = root / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts"
    prompts = {name: (prompts_dir / name).read_text() for name in ("selector.md", "generator.md")}
    cases = specifications()  # Fully specified synthetic inputs BEFORE executing reference code.
    for case in cases:
        calls = []

        def initialize(client, provider="albert", model="", temperature=0.0, system_prompt=None, **kwargs):
            client.provider, client.model, client.temperature = provider, model, temperature
            client.system_prompt, client.timeout = system_prompt, 1

            def create(**request):
                response = case["responses"][len(calls)]
                assert response["provider"] == provider
                payload = {key: request[key] for key in ("messages", "model", "temperature")}
                calls.append(dict(provider=provider, payload=payload, response=response))
                if response["status"] != 200:
                    raise RuntimeError("synthetic provider unavailable")
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response["text"]))])

            client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        def prompt(name, fallback, *, default=None):
            return prompts[name].replace("{today}", case["today"])

        with (
            patch.object(llm_client.LLMClient, "__init__", initialize),
            patch.object(context_selector, "load_prompt", prompt),
            patch.object(generator, "load_prompt", prompt),
        ):
            if case["kind"] == "selector":
                step = context_selector.ContextSelector(
                    config.SelectorConfig(enabled=True, model="primary", prompt_name="selector.md", min_kept_sections=case["floor"])
                )
                result = step.select(case["query"], [models.AggregatedSection(**row) for row in case["sections"]], resolve_ministry(case["ministry"]))
                expected = dict(
                    sections=plain(result),
                    decisions=step.last_decisions,
                    raw=step.last_raw_response,
                    reason=step.last_reasoning,
                    all_rejected=step.all_rejected,
                    prompt_chars=step.last_prompt_chars,
                )
            elif case["kind"] == "generator":
                step = generator.StreamingGenerator(
                    config.GenerationConfig(model="primary", fallback_model="backup", system_prompt_name="generator.md")
                )
                answer, error = None, None
                try:
                    answer = step.generate(case["query"], [models.ContextItem(**row) for row in case["items"]], resolve_ministry(case["ministry"]))
                except RuntimeError as exc:
                    assert str(exc) == "All LLM providers failed"
                    error = type(exc).__name__
                expected = dict(
                    answer=answer,
                    status="failure" if error else "completed",
                    provider=step.provider_used,
                    fallback_count=step.fallback_count,
                    system_prompt=step.last_system_prompt,
                    user_prompt=step.last_full_prompt,
                )
                case["legacy_error_type"] = error
            else:
                # Execute the retained Pipeline.run final-rejection branch. Stub only earlier
                # stages and diagnostics plumbing; no-answer text/control flow remain real.
                step = object.__new__(pipeline.Pipeline)
                step._generator = generator.StreamingGenerator(config.GenerationConfig())
                step._process_query = lambda *args, **kwargs: SimpleNamespace(should_proceed=True, query_for_retrieval=case["query"])

                def final_rejection(qr, state, **kwargs):
                    state.stage_refs["selector_all_rejected"] = True
                    return []

                step._retrieve_and_build = final_rejection
                step._record_generator_event = lambda *args, **kwargs: None
                step._build_result = lambda query, answer, items, qr, **kwargs: SimpleNamespace(answer=answer, timing=kwargs["state"].timing)
                result = step.run(case["query"])
                expected = dict(answer=result.answer, generation_ms=result.timing["generation_ms"], provider_calls=0)
            assert len(calls) == len(case["responses"]), case["id"]
            case["calls"], case["expected"] = calls, expected
    save(output / "prompts.json", prompts)
    save(output / "cases.json", cases)
    sources = list((root / "apps/api/src/assistant_rh_api").rglob("*.py"))
    sources += list((root / "packages/rag-pipeline/src/assistant_rh_rag_pipeline").glob("*.py"))
    save(
        output / "manifest.json",
        dict(
            schema="c5-synthetic-branch-replays-v1",
            head="a8bb4a0b6f78bc5777a99f494582469c198da93a",
            provenance="Synthetic scripted provider replies/outages; expected values executed from retained runtime before API replay",
            files={name: sha(output / name) for name in ("prompts.json", "cases.json")},
            source_hashes={str(path.relative_to(root)): sha(path) for path in sorted(sources)},
        ),
    )


async def replay(root, evidence, mutation=None, *, verify_sources=False):
    import httpx
    from assistant_rh_api.core.errors import InferenceFailure
    from assistant_rh_api.core.models.configuration import Prompt, Snapshot
    from assistant_rh_api.core.models.context import AggregatedSection, ContextItem
    from assistant_rh_api.core.models.rag_configuration import GenerationConfig, SelectorConfig
    from assistant_rh_api.core.pipeline.steps.context_selector import ContextSelector
    from assistant_rh_api.core.pipeline.steps.generator import Generator
    from assistant_rh_api.gateways.chat import ChatGateway
    from assistant_rh_api.gateways.settings import Endpoint, RequestPolicy

    manifest = json.loads((evidence / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert sha(evidence / name) == digest, f"fixture hash mismatch: {name}"
    # Source hashes identify the recording revision. CI must exercise today's
    # implementation against frozen inputs, rather than reject every code edit.
    if verify_sources:
        for name, digest in manifest["source_hashes"].items():
            assert sha(root / name) == digest, f"source changed: {name}"
    prompts = json.loads((evidence / "prompts.json").read_text())
    cases = json.loads((evidence / "cases.json").read_text())
    if mutation:
        mutation(prompts, cases)

    class Store:
        async def get(self, name):
            return Snapshot(Prompt(name, prompts[name]), hashlib.sha256(prompts[name].encode()).hexdigest(), "packaged")

    report = []
    for case in cases:
        seen = []

        def handle(request):
            assert len(seen) < len(case["calls"]), "unexpected extra provider call"
            recorded = case["calls"][len(seen)]
            payload = json.loads(request.content)
            assert payload.pop("stream") is False
            assert request.url.host == recorded["provider"] + ".test", "provider order mismatch"
            assert payload == recorded["payload"], "exact request mismatch"
            seen.append(recorded["provider"])
            response = recorded["response"]
            return httpx.Response(
                response["status"], json={"choices": [{"index": 0, "message": {"content": response["text"]}, "finish_reason": "stop"}]}
            )

        failure = None
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            primary = Endpoint("albert", "primary", "https://albert.test", "synthetic-test-key")
            fallback = None if case["kind"] == "selector" else Endpoint("scaleway", "backup", "https://scaleway.test", "synthetic-test-key")
            gateway = ChatGateway(client, primary, fallback, policy=RequestPolicy(max_attempts=1))
            if case["kind"] == "selector":
                step = ContextSelector(
                    SelectorConfig(enabled=True, model="primary", prompt_name="selector.md", min_kept_sections=case["floor"]),
                    Store(),
                    Store(),
                    gateway,
                )
                result = await step.select(
                    case["query"], tuple(AggregatedSection(**row) for row in case["sections"]), case["ministry"], today=case["today"]
                )
                actual = dict(
                    sections=plain(result.sections),
                    decisions=plain(result.diagnostics.decisions),
                    raw=result.diagnostics.raw_response,
                    reason=plain(result.diagnostics.reason),
                    all_rejected=result.all_rejected,
                    prompt_chars=result.diagnostics.prompt_chars,
                )
                status = result.diagnostics.status
            elif case["kind"] == "generator":
                step = Generator(
                    GenerationConfig(model="primary", fallback_model="backup", system_prompt_name="generator.md"), Store(), Store(), gateway
                )
                try:
                    result = await step.generate(
                        case["query"], tuple(ContextItem(**row) for row in case["items"]), case["ministry"], today=case["today"]
                    )
                    actual = dict(
                        answer=result.answer,
                        status="completed",
                        provider=result.diagnostics.outcome.provider,
                        fallback_count=result.diagnostics.fallback_count,
                        system_prompt=result.diagnostics.request.messages[0].content,
                        user_prompt=result.diagnostics.request.messages[-1].content,
                    )
                except InferenceFailure as exc:
                    assert not exc.partial and len(exc.attempts) == 2
                    assert [a.provider for a in exc.attempts] == ["albert", "scaleway"]
                    assert all(a.error == "unavailable" for a in exc.attempts)
                    failure = dict(type=type(exc).__name__, code=exc.code, partial=exc.partial, attempts=plain(exc.attempts))
                    # Compare the observable failure contract, with the documented RuntimeError
                    # -> InferenceFailure type migration. Requests were already matched exactly.
                    actual = dict(
                        answer=None,
                        status="failure",
                        provider=None,
                        fallback_count=0,
                        system_prompt=case["calls"][0]["payload"]["messages"][0]["content"],
                        user_prompt=case["calls"][0]["payload"]["messages"][-1]["content"],
                    )
                status = actual["status"]
            else:
                step = Generator(GenerationConfig(), Store(), Store(), gateway)
                result = await step.generate(case["query"], (), case["ministry"], today=case["today"], all_rejected=True)
                assert result.diagnostics.outcome is None and result.diagnostics.request is None
                actual = dict(answer=result.answer, generation_ms=0, provider_calls=len(seen))
                status = result.diagnostics.status
            assert actual == case["expected"], f"output mismatch: {case['id']}"
            assert len(seen) == len(case["calls"]), f"missing provider calls: {case['id']}"
            report.append(dict(id=case["id"], exact=True, status=status, provider_sequence=seen, candidate_failure=failure))
    return dict(exact_comparison=True, network="denied", provider_data="synthetic", cases=report)


async def check(root, evidence, *, verify_sources=False):
    controls = []
    mutations = {
        "prompt": lambda prompts, cases: prompts.update({"generator.md": prompts["generator.md"] + " MODIFIED"}),
        "selector-expected": lambda prompts, cases: cases[1]["expected"].update(sections=[{"unexpected": True}]),
        "fallback-route": lambda prompts, cases: cases[6]["calls"][1].update(provider="albert"),
        "fallback-reply": lambda prompts, cases: cases[6]["calls"][1]["response"].update(text="UNSUPPORTED"),
        "no-answer-expected": lambda prompts, cases: cases[8]["expected"].update(answer="UNSUPPORTED"),
    }
    await replay(root, evidence, verify_sources=verify_sources)
    for name, mutation in mutations.items():
        try:
            await replay(root, evidence, mutation, verify_sources=verify_sources)
        except AssertionError as exc:
            controls.append(dict(mutation=name, detected=True, reason=str(exc)))
        else:
            raise AssertionError(f"undetected mutation: {name}")
    return dict(baseline_passed=True, controls=controls)


def main():
    import socket
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["record", "replay", "check"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--verify-source-hashes", action="store_true", help="also require the exact code revision used to record")
    args = parser.parse_args()
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for path in ("apps/api/src", "packages/rag-pipeline/src", "packages/shared-config/src", "packages/data-engineering/src"):
        sys.path.insert(0, str(args.root / path))
    logging.disable(logging.CRITICAL)
    with ExitStack() as stack:
        for method in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, method, side_effect=AssertionError("network denied")))
        stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("network denied")))
        if args.mode == "record":
            record(args.root, args.evidence)
            print(json.dumps({"recorded_synthetic_cases": 9, "network": "denied"}))
        else:
            action = replay if args.mode == "replay" else check
            result = asyncio.run(action(args.root, args.evidence, verify_sources=args.verify_source_hashes))
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
