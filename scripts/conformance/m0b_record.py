"""Fresh staging recording; the historical M0b reference is never overwritten."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from scripts.conformance.m0b_legacy_inputs import attach_content, attach_search, recording_cursor
from scripts.conformance.m0b_private import private_directory
from scripts.conformance.m0b_replay import compare, run_candidate
from scripts.conformance.m0b_values import (
    BASELINE,
    PANEL,
    ROOT,
    RecordedStore,
    ReplayStore,
    Tape,
    aggregation_output,
    context_output,
    digest,
    dump,
    final_output,
    plain,
    query_output,
    require,
    selection_output,
)


def configure(env_file):
    from dotenv import dotenv_values
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = dotenv_values(env_file)
    staging, production = values.get("SCW_POSTGRES_DSN_STAGING"), values.get("SCW_POSTGRES_DSN_PROD")
    require(staging and production, "Explicit staging and production identities required")
    target, other = conninfo_to_dict(staging), conninfo_to_dict(production)
    require(all(target.get(k) for k in ("host", "port", "dbname", "user")) and "service" not in target, "Companion invariant violated")
    require(
        tuple(target.get(k) for k in ("host", "port", "dbname")) != tuple(other.get(k) for k in ("host", "port", "dbname")),
        "Companion invariant violated",
    )
    for key in list(os.environ):
        if key.startswith("PG") or key in ("SCW_POSTGRES_DSN", "APP_POSTGRES_DSN", "STREAMLIT_POSTGRES_DSN"):
            del os.environ[key]
    for key in ("ALBERT_API_KEY", "ALBERT_BASE_URL", "ALBERT_EMBED_MODEL", "ALBERT_RERANK_MODEL", "SCALEWAY_API_KEY", "SCALEWAY_BASE_URL"):
        if values.get(key):
            os.environ[key] = values[key]
    dsn = make_conninfo(
        staging,
        sslmode="require",
        connect_timeout="10",
        options="-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000",
    )
    os.environ.update(
        SCW_POSTGRES_DSN=dsn,
        APP_DB_TARGET="scaleway",
        APP_SCALEWAY_ENV="staging",
        APP_ENV="staging",
        PYTHON_DOTENV_DISABLED="1",
        OTEL_SDK_DISABLED="true",
    )
    secrets = [values[k] for k in ("ALBERT_API_KEY", "SCALEWAY_API_KEY", "SCW_POSTGRES_DSN_STAGING", "SCW_POSTGRES_DSN_PROD") if values.get(k)]
    return dsn, secrets


class SnapshotDatabase:
    def __init__(self, dsn, snapshot):
        self.dsn, self.snapshot = dsn, snapshot
        self.capacity = asyncio.Semaphore(4)

    @asynccontextmanager
    async def transaction(self, *, read_only=False):
        import psycopg
        from psycopg import sql

        require(read_only, "Companion must not write to staging")
        async with self.capacity, await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(self.snapshot)))
            yield conn


def record_legacy(case, dsn, config):
    from assistant_rh_rag_pipeline import context_selector, db_helpers, embedder, llm_client, reranker
    from assistant_rh_rag_pipeline.ministry_scope import build_retrieval_scope
    from assistant_rh_rag_pipeline.pipeline import Pipeline

    events, inference = case["expected"]["stages"], case["inference"]
    original_chat = llm_client.LLMClient.chat
    original_embed = embedder.FallbackEmbedder.embed_query
    original_rank = reranker.AlbertReranker.rerank
    original_select = context_selector.ContextSelector.select
    fixed_date = datetime.fromisoformat(case["today"] + "T12:00:00")

    class FrozenDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_date if tz is None else fixed_date.replace(tzinfo=timezone.utc).astimezone(tz)

    def chat(client, prompt, system_prompt=None):
        original_create = client._client.chat.completions.create

        def create(*args, **kwargs):
            response = original_create(*args, **kwargs)
            inference.append(
                {
                    "operation": "llm.complete",
                    "request": {
                        "provider": client.provider,
                        "model": client.model,
                        "temperature": client.temperature,
                        "messages": plain(kwargs["messages"]),
                    },
                    "response": {"response": response.model_dump(mode="json"), "text": (response.choices[0].message.content or "").strip()},
                }
            )
            return response

        with patch.object(client._client.chat.completions, "create", create):
            return original_chat(client, prompt, system_prompt)

    def embed(client, text):
        result = original_embed(client, text)
        require(result is not None, "A successful recorded embedding is required")
        model = client.last_model_used
        inference.append(
            {
                "operation": "embeddings.embed",
                "request": {"text": text},
                "response": {"vector": plain(result), "model": model, "provider": "albert" if model == "albert" else "scaleway"},
            }
        )
        return result

    def rank(client, query, texts, top_k=None):
        http_calls = []
        original_post = client._post

        def post(*args, **kwargs):
            response = original_post(*args, **kwargs)
            http_calls.append({"request": plain(kwargs["json"]), "status": response.status_code, "response": response.json()})
            return response

        with patch.object(client, "_post", post):
            result = original_rank(client, query, texts, top_k)
        inference.append(
            {
                "operation": "reranker.rerank",
                "request": {"query": query, "documents": list(texts), "top_k": top_k},
                "response": {"result": plain(result), "http": http_calls},
            }
        )
        return result

    def select(selector, *args, **kwargs):
        result = original_select(selector, *args, **kwargs)
        events.append(
            {
                "stage": "context-selector",
                "output": selection_output(
                    result,
                    decisions=selector.last_decisions,
                    raw_response=selector.last_raw_response,
                    reason=selector.last_reasoning,
                    all_rejected=selector.all_rejected,
                    prompt_chars=selector.last_prompt_chars,
                ),
            }
        )
        return result

    def observe(owner, name, stage, project):
        original = getattr(owner, name)

        def call(*args, **kwargs):
            result = original(*args, **kwargs)
            events.append({"stage": stage, "output": project(result)})
            return result

        setattr(owner, name, call)

    with (
        patch.object(llm_client.LLMClient, "chat", chat),
        patch.object(embedder.FallbackEmbedder, "embed_query", embed),
        patch.object(reranker.AlbertReranker, "rerank", rank),
        patch.object(context_selector.ContextSelector, "select", select),
        patch.object(db_helpers, "datetime", FrozenDate),
    ):
        pipe = Pipeline(config, dsn=dsn)
        attach_search(pipe._retriever, case["stores"])
        attach_content(pipe._aggregator, pipe._context_builder, case["stores"])
        observe(pipe._query_processor, "process", "query-processor", query_output)
        observe(pipe._retriever, "retrieve", "retriever", plain)
        observe(pipe._aggregator, "aggregate_with_diagnostics", "section-aggregator", aggregation_output)
        builder = pipe._context_builder
        observe(
            builder, "build", "context-builder", lambda items: context_output(items, builder.last_resolved_refs, builder.format_for_prompt(items))
        )
        generator = pipe._generator
        observe(
            generator,
            "generate",
            "generator",
            lambda answer: {
                "answer": answer,
                "system_prompt": generator.last_system_prompt,
                "user_prompt": generator.last_full_prompt,
                "provider": generator.provider_used,
                "fallback_count": generator.fallback_count,
            },
        )
        fixture = case["fixture"]
        result = pipe.run(
            fixture["query"],
            fixture.get("conversation_history"),
            include_stage_trace=True,
            retrieval_scope=build_retrieval_scope(case["effective_ministry"]),
        )
        case["expected"]["result"] = final_output(result.answer, result.context_items, result.sources, result.metadata)
        case["legacy_stage_trace"] = result.metadata["stage_trace"]
        case["observed"] = {
            key: result.metadata.get(key)
            for key in ("intent", "selected_ministry", "selector_all_rejected", "selector_retry_triggered", "generator_used_fallback")
        }


async def record(output, env_file, limit=None):
    import psycopg
    from psycopg import sql

    require(not output.exists(), "Recording output must be a new private directory")
    output = private_directory(output)
    logging.basicConfig(filename=output / "private-record.log", level=logging.WARNING)
    dsn, secrets = configure(env_file)
    from assistant_rh_api.db.search_catalog import search_catalog
    from assistant_rh_api.db.settings_stores import AcronymStore, ConfigStore, PromptStore
    from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore
    from assistant_rh_rag_pipeline.admin import RuntimeRAGConfig, runtime_config_to_rag_config

    fixtures = [json.loads(line) for line in PANEL.read_text().splitlines()]
    if limit is not None:
        fixtures = fixtures[:limit]
    real_connect = psycopg.connect
    cursor_factory = recording_cursor()
    with real_connect(dsn) as anchor:
        anchor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = anchor.execute("SELECT pg_export_snapshot()").fetchone()[0]
        require(anchor.execute("SHOW transaction_read_only").fetchone() == ("on",), "Companion invariant violated")
        raw_config = anchor.execute("SELECT config FROM public.rag_config WHERE id = 1").fetchone()[0]
        config = runtime_config_to_rag_config(RuntimeRAGConfig.from_dict(raw_config))
        identity = anchor.execute("SELECT version(), (SELECT count(*) FROM rag_documents), (SELECT count(*) FROM rag_sections)").fetchone()
        database = SnapshotDatabase(dsn, snapshot)

        def connect(*args, **kwargs):
            # All legacy helpers join the same read-only staging snapshot.
            kwargs.pop("conninfo", None)
            kwargs["cursor_factory"] = cursor_factory
            conn = real_connect(dsn, **kwargs)
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
            return conn

        reports, paths = [], []
        with patch.object(psycopg, "connect", connect):
            for fixture in fixtures:
                print("Recording " + fixture["id"], flush=True)
                case = {
                    "fixture": fixture,
                    "effective_ministry": fixture.get("ministry") or "matte",
                    "today": datetime.now(timezone.utc).date().isoformat(),
                    "config": config.to_dict(),
                    "catalogue": plain([table.source for table in search_catalog()]),
                    "inference": [],
                    "stores": [],
                    "expected": {"stages": []},
                }
                await asyncio.to_thread(record_legacy, case, dsn, config)
                underlying = {
                    "config": ConfigStore(database),
                    "prompts": PromptStore(database),
                    "acronyms": AcronymStore(database),
                    "packaged": PackagedPromptStore(),
                }
                stores = {name: RecordedStore(name, store, case["stores"]) for name, store in underlying.items()}
                legacy_inputs = Tape(case["stores"])
                stores.update({name: ReplayStore(name, legacy_inputs) for name in ("search", "content")})
                path = output / (fixture["id"] + ".json")
                try:
                    actual = await run_candidate(case, stores)
                    legacy_inputs.assert_consumed()
                    dump(output / (fixture["id"] + ".actual.json"), actual)
                    report = compare(case, actual)
                    reports.append(report)
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                finally:
                    dump(path, case)
                paths.append(path)
    for path in paths:
        text = path.read_text()
        require(not any(secret in text for secret in secrets), "Secret found in recording")
        require("X-Amz-Signature" not in text and "X-Amz-Credential" not in text, "Signed storage URL found in recording")
    sources = {}
    for folder in ("packages/rag-pipeline/src", "apps/api/src", "scripts/conformance"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            sources[str(path.relative_to(ROOT))] = digest(path)
    manifest = {
        "schema": "m0b-full-pipeline-companion-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_revision_status": "working_tree_snapshot; source_hashes are authoritative, source_commit is only the checkout base",
        "scope": (
            "Fresh retained-runtime vs production API composition on the seven M0b scenarios; "
            "not recovery of September 1 inputs or a live quality evaluation"
        ),
        "database": {
            "target": "explicit_staging",
            "read_only": True,
            "shared_snapshot": snapshot,
            "server_version": identity[0],
            "documents": identity[1],
            "sections": identity[2],
        },
        "panel_sha256": digest(PANEL),
        "original_manifest_sha256": digest(BASELINE / "manifest.json"),
        "source_hashes": sources,
        "files": {path.name: digest(path) for path in paths},
        "query_count": len(paths),
        "complete_panel": len(paths) == 7,
    }
    dump(output / "manifest.json", manifest)
    dump(output / "record-comparison.json", {"exact_comparison": len(paths) == 7, "cases": reports})
    return manifest
