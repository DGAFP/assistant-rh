"""Execute the production composition with recorded ports and no remote writes."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from scripts.conformance.m0b_values import (
    Tape,
    aggregation_output,
    assert_equal,
    context_output,
    final_output,
    legacy_sources,
    plain,
    query_output,
    require,
    selection_output,
)


class Inference:
    def __init__(self, tape, provider="albert", model=""):
        self.tape, self.provider, self.model = tape, provider, model

    async def complete(self, request):
        from assistant_rh_api.core.models.inference import Attempt, Completion, TokenUsage

        call = self.tape.take(
            "llm.complete",
            {
                "provider": self.provider,
                "model": self.model,
                "temperature": request.temperature,
                "messages": plain(request.messages),
            },
        )
        usage = call["response"].get("usage")
        usage = TokenUsage(*(usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens"))) if usage else None
        return Completion(
            call["text"], self.provider, self.model, (Attempt(self.provider, self.model),), call["response"]["choices"][0]["finish_reason"], usage
        )

    async def embed(self, text):
        from assistant_rh_api.core.models.inference import Embedding

        value = self.tape.take("embeddings.embed", {"text": text})
        return Embedding(tuple(value["vector"]), value["model"], value["provider"], ())

    async def rerank(self, query, documents, *, top_k=None):
        from assistant_rh_api.core.models.inference import RankedDocument, Reranking

        value = self.tape.take("reranker.rerank", {"query": query, "documents": list(documents), "top_k": top_k})
        return Reranking(tuple(RankedDocument(*row) for row in value["result"]), (), False)


def candidate_stage(name, result):
    from assistant_rh_api.core.pipeline.steps.context_formatting import format_for_prompt

    if name == "query-processor":
        return query_output(result.result)
    if name == "retriever":
        return plain(result.chunks)
    if name == "section-aggregator":
        return aggregation_output(result)
    if name == "context-selector":
        d = result.diagnostics
        return selection_output(
            result.sections,
            decisions=d.decisions,
            raw_response=d.raw_response,
            reason=d.reason,
            all_rejected=result.all_rejected,
            prompt_chars=d.prompt_chars,
        )
    if name == "context-builder":
        return context_output(result.items, result.resolved_refs, format_for_prompt(result.items))
    if name == "generator":
        d = result.diagnostics
        return {
            "answer": result.answer,
            "system_prompt": d.request.messages[0].content if d.request else "",
            "user_prompt": d.request.messages[-1].content if d.request else "",
            "provider": d.outcome.provider if d.outcome else None,
            "fallback_count": d.fallback_count,
        }
    raise AssertionError(f"Unexpected stage: {name}")


@contextmanager
def deny_network():
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden during replay")

    with (
        patch.object(socket.socket, "connect", denied),
        patch.object(socket.socket, "connect_ex", denied),
        patch.object(socket, "create_connection", denied),
    ):
        yield


async def run_candidate(case, stores):
    from assistant_rh_api import bootstrap
    from assistant_rh_api.core import chat
    from assistant_rh_api.core.auth import AuthContext
    from assistant_rh_api.core.models.auth import Group, Session
    from assistant_rh_api.core.models.chat import ChatInput, RunContext
    from assistant_rh_api.core.models.inference import Message
    from assistant_rh_api.core.rag_configuration import RAGConfigurationService
    from assistant_rh_api.db.search_catalog import search_catalog

    assert_equal(plain([table.source for table in search_catalog()]), case["catalogue"], "source catalogue")
    stages, finalized, failures = [], [], []
    inference = Tape(case["inference"])
    created = datetime.fromisoformat(case["today"] + "T12:00:00+00:00")
    clock = SimpleNamespace(now=lambda: created, monotonic=lambda: 0.0)
    fixture = case["fixture"]
    ministry = case["effective_ministry"]
    ids = iter(("1" * 32, "2" * 32))

    class ObservedContext(RunContext):
        async def stage(self, name, operation, *, project, measure=None, attempt=""):
            try:
                result = await super().stage(name, operation, project=project, measure=measure, attempt=attempt)
            except Exception as exc:
                failures.append(exc)
                raise
            if name == "configuration":
                assert_equal(result.config.value.to_dict(), case["config"], "effective configuration")
            else:
                stages.append({"stage": name, "output": candidate_stage(name, result)})
                try:
                    assert_equal(stages[-1], case["expected"]["stages"][len(stages) - 1], case["fixture"]["id"] + " " + name)
                except AssertionError as exc:
                    exc.actual_stage = stages[-1]
                    failures.append(exc)
                    raise
            return result

    class Runs:
        async def finalize(self, run):
            finalized.append(run)

    replacements = {
        "SearchStore": lambda *args: stores["search"],
        "ContentStore": lambda *args: stores["content"],
        "PromptStore": lambda *args: stores["prompts"],
        "AcronymStore": lambda *args: stores["acronyms"],
        "PackagedPromptStore": lambda: stores["packaged"],
        "EmbeddingGateway": lambda *args, **kwargs: Inference(inference),
        "ChatGateway": lambda client, primary, fallback=None: Inference(inference, primary.provider, primary.model),
        "RerankerGateway": lambda *args: Inference(inference),
        "ChatRunStore": lambda *args, **kwargs: Runs(),
        "SystemClock": lambda: clock,
        "RunIds": lambda: SimpleNamespace(new_id=lambda: next(ids)),
    }
    environment = {"ALBERT_API_KEY": "offline-fixture", "SCALEWAY_API_KEY": "offline-fixture"}
    auth = AuthContext(
        Group(
            slug="conformance",
            label="Conformance",
            priority=0,
            visible=True,
            is_admin=False,
            password_hash="synthetic",
            allowed_ministries=(ministry,),
            default_ministry=ministry,
        ),
        Session(
            token_hash="a" * 64, group_slug="conformance", created_at=created, expires_at=created + timedelta(hours=8), credential_hash="synthetic"
        ),
    )
    request = ChatInput(
        model=f"assistant-rh-{ministry}", question=fixture["query"], history=tuple(Message(**row) for row in fixture.get("conversation_history", []))
    )
    with ExitStack() as patches:
        for name, replacement in replacements.items():
            patches.enter_context(patch.object(bootstrap, name, replacement))
        patches.enter_context(patch.object(chat, "RunContext", ObservedContext))
        service = bootstrap.create_chat_service(None, RAGConfigurationService(stores["config"]), None, environment)
        try:
            run, result = await service.complete(request, auth)
        except Exception:
            if failures:
                raise failures[0] from None
            raise
    inference.assert_consumed()
    require(len(finalized) == 1 and finalized[0] == run and run.status == "completed", "one completed finalization required")
    return {
        "stages": stages,
        "result": final_output(result.answer, result.items, legacy_sources(result.items), run.diagnostics),
        "api": {
            "model": run.model,
            "ministry": run.selected_ministry,
            "sources": plain(run.sources),
            "answer": run.answer,
            "usage": plain(result.usage),
            "event_count": len(run.events),
        },
    }


def compare(case, actual):
    assert_equal(actual["stages"], case["expected"]["stages"], case["fixture"]["id"] + " stages")
    assert_equal(actual["result"], case["expected"]["result"], case["fixture"]["id"] + " result")
    return {
        "id": case["fixture"]["id"],
        "exact": True,
        "stages": len(actual["stages"]),
        "source_count": len(actual["result"]["sources"]),
        "answer_chars": len(actual["result"]["answer"]),
        "inference_calls": len(case["inference"]),
        "store_calls": len(case["stores"]),
        "api_model": actual["api"]["model"],
        "events": actual["api"]["event_count"],
    }


async def replay_case(case):
    from scripts.conformance.m0b_values import ReplayStore

    tape = Tape(case["stores"])
    stores = {name: ReplayStore(name, tape) for name in ("config", "search", "content", "prompts", "packaged", "acronyms")}
    with deny_network():
        actual = await run_candidate(case, stores)
    tape.assert_consumed()
    return compare(case, actual)
