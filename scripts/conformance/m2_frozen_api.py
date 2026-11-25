"""Record a private corpus run at retrieval output, then replay core and HTTP offline."""

import argparse
import asyncio
import hashlib
import json
import os
from contextlib import ExitStack
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from assistant_rh_api import bootstrap
from assistant_rh_api.core import chat
from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.auth import Group, Session
from assistant_rh_api.core.models.chat import ChatInput, RunContext
from assistant_rh_api.core.rag_configuration import RAGConfigurationService
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.settings_stores import ConfigStore
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from scripts.conformance.m0b_replay import candidate_stage, compare, replay_candidate
from scripts.conformance.m0b_values import RecordedStore, assert_equal, final_output, legacy_sources, plain, require


class ChatRecorder:
    def __init__(self, gateway, endpoint, records):
        self.gateway = gateway
        self.endpoint = endpoint
        self.records = records

    async def complete(self, request):
        call = {
            "operation": "llm.complete",
            "request": {
                "provider": self.endpoint.provider,
                "model": self.endpoint.model,
                "temperature": request.temperature,
                "messages": plain(request.messages),
            },
        }
        try:
            result = await self.gateway.complete(request)
        except InferenceFailure as error:
            call["response"] = {"typed_failure": {"attempts": plain(error.attempts), "partial": error.partial}}
            self.records.append(call)
            raise
        call["response"] = {"typed_completion": plain(result)}
        self.records.append(call)
        return result


class RerankRecorder:
    def __init__(self, gateway, records):
        self.gateway = gateway
        self.records = records

    async def rerank(self, query, documents, *, top_k=None):
        result = await self.gateway.rerank(query, documents, top_k=top_k)
        self.records.append(
            {
                "operation": "reranker.rerank",
                "request": {"query": query, "documents": list(documents), "top_k": top_k},
                "response": {"typed_reranking": plain(result)},
            }
        )
        return result


async def capture_case(row, database, client, environment, today):
    stores, inference, stages, persisted = [], [], [], []
    created = datetime.combine(date.fromisoformat(today), time(12), tzinfo=UTC)
    clock = SimpleNamespace(now=lambda: created, monotonic=lambda: 0.0)
    ministry = row["model"].removeprefix("assistant-rh-")
    config_store = RecordedStore("config", ConfigStore(database), stores)

    class ObservedContext(RunContext):
        async def stage(self, name, operation, *, project, measure=None, attempt=""):
            result = await super().stage(name, operation, project=project, measure=measure, attempt=attempt)
            if name == "configuration":
                snapshot.update(result.config.value.to_dict())
            else:
                stages.append({"stage": name, "output": candidate_stage(name, result)})
            return result

    class LocalRuns:
        async def finalize(self, run):
            persisted.append(run)

    snapshot = {}
    store_types = {
        "ContentStore": "content",
        "PromptStore": "prompts",
        "AcronymStore": "acronyms",
        "PackagedPromptStore": "packaged",
        "Retriever": "retriever",
    }
    real_chat, real_reranker = bootstrap.ChatGateway, bootstrap.RerankerGateway
    with ExitStack() as patches:
        for name, prefix in store_types.items():
            factory = getattr(bootstrap, name)

            def record_store(*args, _factory=factory, _prefix=prefix, **kwargs):
                return RecordedStore(_prefix, _factory(*args, **kwargs), stores)

            patches.enter_context(patch.object(bootstrap, name, record_store))
        patches.enter_context(
            patch.object(
                bootstrap, "ChatGateway", lambda c, primary, fallback=None: ChatRecorder(real_chat(c, primary, fallback), primary, inference)
            )
        )
        patches.enter_context(patch.object(bootstrap, "RerankerGateway", lambda *args: RerankRecorder(real_reranker(*args), inference)))
        patches.enter_context(patch.object(bootstrap, "ChatRunStore", lambda *args, **kwargs: LocalRuns()))
        patches.enter_context(patch.object(bootstrap, "SystemClock", lambda: clock))
        patches.enter_context(patch.object(chat, "RunContext", ObservedContext))
        service = bootstrap.create_chat_service(database, RAGConfigurationService(config_store), client, environment)
        auth = AuthContext(
            Group("conformance", "Conformance", 0, True, False, "fixture", (ministry,), ministry),
            Session("a" * 64, "conformance", created, created + timedelta(hours=1), "fixture"),
        )
        request = ChatInput(row["model"], row["question"])
        run, result = await service.complete(request, auth)
    require(len(persisted) == 1 and persisted[0] == run and run.status == "completed", "Expected one in-memory capture")
    return {
        "schema": "m2-frozen-api-v1",
        "retrieval_boundary": "retriever_output",
        "fixture": {"id": f"q{row['id']}", "query": row["question"]},
        "today": today,
        "effective_ministry": ministry,
        "config": snapshot,
        "catalogue": plain([table.source for table in bootstrap.search_catalog()]),
        "stores": stores,
        "inference": inference,
        "expected": {
            "stages": stages,
            "result": final_output(result.answer, result.items, legacy_sources(result.items), run.diagnostics),
        },
        "api": {
            "model": run.model,
            "ministry": run.selected_ministry,
            "sources": plain(run.sources),
            "answer": run.answer,
            "usage": plain(result.usage),
            "event_count": len(run.events),
        },
    }


async def capture_staging(panel, today):
    environment = dict(os.environ)
    target = conninfo_to_dict(environment["SCW_POSTGRES_DSN"])
    require(environment["APP_ENV"] == "staging" and target["host"] == "10.10.1.2", "Expected the private Kapsule staging database")
    require(not environment.get("SCALEWAY_API_KEY"), "The capture permits Albert DINUM only")
    albert = urlsplit(environment["ALBERT_BASE_URL"])
    require(albert.scheme == "https" and albert.hostname == "albert.api.etalab.gouv.fr", "Unexpected inference destination")
    dsn = make_conninfo(environment["SCW_POSTGRES_DSN"], options="-c default_transaction_read_only=on")
    database = Database(DatabaseSettings(dsn=dsn, max_size=3))

    async def enforce_destination(request):
        require(request.url.scheme == "https" and request.url.host == "albert.api.etalab.gouv.fr", "Inference destination outside the capture scope")

    await database.open()
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, event_hooks={"request": [enforce_destination]}) as client:
            for row in panel:
                case = await capture_case(row, database, client, environment, today)
                print(json.dumps(case, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        await database.close()


async def replay(path, expected_count, sha256):
    from scripts.conformance.m2_http_replay import complete_over_http

    with path.open("rb") as source:
        hasher = hashlib.file_digest(source, "sha256").hexdigest()
    require(hasher == sha256, "Capture archive fingerprint mismatch")
    seen = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            case = json.loads(line)
            case_id = case["fixture"]["id"]
            require(case["schema"] == "m2-frozen-api-v1" and case_id not in seen, "Invalid capture inventory")
            seen.add(case_id)
            direct = await replay_candidate(case)
            via_http = await replay_candidate(case, complete=complete_over_http)
            compare(case, direct)
            assert_equal(direct["api"], case["api"], case_id + " recorded API result")
            assert_equal(via_http, direct, case_id + " HTTP vs direct core")
    require(len(seen) == expected_count, "Incomplete capture")
    return {"cases": len(seen), "sha256": hasher, "http_json_exact": True, "network": "denied"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", type=int, default=98)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(replay(args.archive, args.count, args.sha256))))


if __name__ == "__main__":
    main()
