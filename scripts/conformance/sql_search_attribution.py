"""Controlled API SQL intervention: real read-only stores, frozen inference I/O.

Run inside the staging API environment with a private manifest on stdin. Output
JSONL contains private corpus/prompts/responses, never HTTP authorization headers.
The old pool/search sources are supplied explicitly, not installed into the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import types
from collections import defaultdict
from collections.abc import Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from unittest.mock import patch

import httpx


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def request_identity(method, url, body):
    """Canonical JSON keys only; every model, prompt, vector and option counts."""
    return digest({"method": method, "url": url, "body": json.loads(body)})


class RecordingStream(httpx.AsyncByteStream):
    """Observe chunks as consumed, preserving mid-stream failures and closing."""

    def __init__(self, stream, row):
        self.stream = stream
        self.row = row

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.row["chunks"].append(chunk.hex())
                yield chunk
        except BaseException as error:
            self.row["stream_error_type"] = type(error).__name__
            raise
        else:
            self.row["response"] = b"".join(bytes.fromhex(c) for c in self.row["chunks"]).decode()
            self.row["complete"] = not self.row["content_type"].startswith("text/event-stream")

    async def aclose(self):
        await self.stream.aclose()


class ReplayStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield bytes.fromhex(chunk)


class InferenceTape(httpx.AsyncBaseTransport):
    def __init__(self, rows=None, *, allowed_hosts=frozenset({"albert.api.etalab.gouv.fr"}), allow_live=False):
        if rows is None and not allow_live:
            raise PermissionError("Explicit live-inference approval required")
        self.allowed_hosts = allowed_hosts
        self.rows = [] if rows is None else rows
        self.record = rows is None
        self.network = httpx.AsyncHTTPTransport() if self.record else None
        self.used = defaultdict(int)
        self.live_requests = {}

    async def handle_async_request(self, request):
        assert request.url.scheme == "https" and not request.url.query and not request.url.username
        assert request.url.host in self.allowed_hosts, "Unapproved inference destination"
        body = (await request.aread()).decode()
        url = str(request.url)
        key = request_identity(request.method, url, body)
        if self.record:
            response = await self.network.handle_async_request(request)
            row = {
                "stage": getattr(self, "stage", ""),
                "key": key,
                "method": request.method,
                "url": url,
                "body": body,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "response": "",
                "chunks": [],
                "complete": False,
            }
            self.rows.append(row)
            # Credentials exist only in live memory, never in serialized rows.
            self.live_requests[key] = request
            if response.is_stream_consumed:
                row.update(
                    chunks=[response.content.hex()],
                    response=response.content.decode(),
                    complete=not row["content_type"].startswith("text/event-stream"),
                )
            else:
                response.stream = RecordingStream(response.stream, row)
            return response
        else:
            matches = [r for r in self.rows if r["key"] == key]
            ordinal = self.used[key]
            if ordinal >= len(matches):
                raise AssertionError("Unrecorded or extra inference request: " + key)
            row = matches[ordinal]
            if request_identity(row["method"], row["url"], row["body"]) != key:
                raise AssertionError("Altered tape request")
            if not row.get("complete"):
                raise AssertionError("Incomplete inference capture cannot establish parity")
            self.used[key] += 1
        chunks = row.get("chunks", [row["response"].encode().hex()])
        return httpx.Response(row["status"], stream=ReplayStream(chunks), headers={"content-type": row["content_type"]}, request=request)

    @asynccontextmanager
    async def observe_chat_stream(self, stream_context):
        """Use the real gateway's validated terminal event, never HTTP EOF for SSE."""
        from assistant_rh_api.core.models.inference import StreamCompleted

        async with stream_context as events:

            async def observe():
                async for event in events:
                    if self.record and isinstance(event, StreamCompleted):
                        row = self.rows[-1]
                        assert json.loads(row["body"])["stream"] is True
                        row["response"] = b"".join(bytes.fromhex(c) for c in row["chunks"]).decode()
                        row["complete"] = True
                    yield event

            async with aclosing(observe()) as observed:
                yield observed

    def assert_consumed(self):
        assert not self.record
        assert sum(self.used.values()) == len(self.rows), "Unused recorded inference requests"

    async def aclose(self):
        if self.network:
            await self.network.aclose()


async def evaluate(manifest, emit):
    if manifest.get("allow_live_inference") is not True:
        raise PermissionError("Explicit live-inference approval required")
    from assistant_rh_api import bootstrap
    from assistant_rh_api.core.models.chat import ChatInput, RunContext
    from assistant_rh_api.core.rag_configuration import RAGConfigurationService
    from assistant_rh_api.db.dsn import DatabaseSettings
    from assistant_rh_api.db.pool import Database
    from assistant_rh_api.db.settings_stores import ConfigStore
    from assistant_rh_api.gateways.auth import SystemClock
    from assistant_rh_api.gateways.chat import ChatGateway
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ["SCW_POSTGRES_DSN"]
    assert conninfo_to_dict(dsn)["host"] == manifest["expected_database_host"]
    baseline = {}
    for name in ("pool", "search_store"):
        module = types.ModuleType("sql_attribution_baseline_" + name)
        sys.modules[module.__name__] = module
        exec(manifest["baseline_sources"][name], module.__dict__)
        baseline[name] = module

    @dataclass
    class Captured(RunContext):
        outputs: dict = field(default_factory=dict)

        async def stage(self, name, operation, *, project, measure=None, attempt=""):
            self.tape.stage = name + ":" + attempt
            result = await super().stage(name, operation, project=project, measure=measure, attempt=attempt)
            if name == "retriever":
                value = {
                    "chunks": result.chunks,
                    "sources": result.sources,
                    "failures": result.failures,
                    "embedding": result.embedding,
                    "embedding_failed": result.embedding_failed,
                }
            elif name in ("section-aggregator", "context-selector"):
                value = {"sections": result.sections, "diagnostics": result.diagnostics}
                if name == "context-selector":
                    value["all_rejected"] = result.all_rejected
            else:
                value = result
            self.outputs[name + ":" + attempt] = plain(value)
            return result

    async def snapshot(database):
        from psycopg import sql

        tables = {
            "config": ("rag_config", "id"),
            "prompts": ("system_prompts", "name"),
            "acronyms": ("acronyms", "id"),
            "documents": ("rag_documents", "doc_id"),
            "sections": ("rag_sections", "section_id"),
        }
        for source in bootstrap.search_catalog():
            tables[source.source.key] = (source.source.name, source.id_column)
        out = {}
        async with database.transaction(read_only=True) as connection:
            out["role"] = (await (await connection.execute("SELECT current_user")).fetchone())[0]
            for name, (table, key) in tables.items():
                query = sql.SQL("SELECT count(*), md5(string_agg(md5(row_to_json(t)::text), '' ORDER BY {key})) FROM {table} t").format(
                    key=sql.Identifier(key), table=sql.Identifier("public", table)
                )
                out[name] = await (await connection.execute(query)).fetchone()
        return out

    old = baseline["pool"].Database(DatabaseSettings(dsn=dsn))
    new = Database(DatabaseSettings(dsn=dsn))
    await old.open()
    await new.open()
    clock = SystemClock()
    created = datetime.fromisoformat(manifest["created_at"])
    try:
        initial = await snapshot(new)
        configuration = await RAGConfigurationService(ConfigStore(new)).load()
        assert configuration.fallback is None
        config = configuration.config.value
        emit({"kind": "started", "snapshot": initial, "configuration": plain(config), "run_label": manifest["run_label"]})

        async def run(case, arm, tape):
            db = old if arm == "baseline" else new
            store = baseline["search_store"].SearchStore if arm == "baseline" else bootstrap.SearchStore
            async with httpx.AsyncClient(transport=tape, timeout=90) as client:
                with patch.object(bootstrap, "SearchStore", store):
                    service = bootstrap.create_chat_service(db, RAGConfigurationService(ConfigStore(db)), client, os.environ)
                pipeline = service._pipeline_factory(config)
                context = Captured(
                    turn_id="sql-attribution", trace_id="sql-attribution", created=created, today=created.date().isoformat(), clock=clock
                )
                context.tape = tape
                original_stream = ChatGateway.stream
                with patch.object(ChatGateway, "stream", lambda gateway, request: tape.observe_chat_stream(original_stream(gateway, request))):
                    result = await asyncio.wait_for(
                        pipeline.run(ChatInput("assistant-rh-" + case["ministry"], case["question"]), case["ministry"], context, stream=True),
                        timeout=120,
                    )
                return {"stages": context.outputs, "items": plain(result.items), "answer": result.answer, "diagnostics": plain(context.diagnostics)}

        for case in manifest["cases"]:
            record = InferenceTape(allow_live=True)
            captured = await run(case, "baseline", record)
            rows = record.rows
            emit({"kind": "capture", "case_id": case["id"], "capture": captured, "tape": rows})
            assert all(r["complete"] and 200 <= r["status"] < 300 for r in rows), "Incomplete or failed capture excluded from semantic parity"
            # Match capture back to itself before comparing the intervention.
            replay_old, replay_new = InferenceTape(rows), InferenceTape(rows)
            before = await run(case, "baseline", replay_old)
            replay_old.assert_consumed()
            after = await run(case, "candidate", replay_new)
            replay_new.assert_consumed()
            equal = captured == before == after
            result = {
                "kind": "case",
                "case": case,
                "capture": captured,
                "baseline_replay": before,
                "candidate_replay": after,
                "tape": rows,
                "exact": equal,
                "stage_equal": {k: captured["stages"][k] == after["stages"].get(k) for k in captured["stages"]},
                "capture_sha256": digest(captured),
                "baseline_sha256": digest(before),
                "candidate_sha256": digest(after),
            }
            emit(result)
            assert equal, "First full-pipeline divergence in case " + str(case["id"])

            if case["id"] in manifest["variance_case_ids"]:
                # Replay exactly the captured HTTP request, including raw bytes,
                # only for reranking/selection/generation, without SQL or prompt reconstruction.
                probes = []
                for row in rows:
                    if row["stage"].split(":")[0] not in {"section-aggregator", "context-selector", "generator"}:
                        continue
                    original = record.live_requests[row["key"]]
                    variants = [{"status": row["status"], "response": row["response"]}]
                    async with httpx.AsyncClient(timeout=90) as client:
                        for _ in range(manifest["variance_repeats"]):
                            request = httpx.Request(row["method"], row["url"], headers=original.headers, content=row["body"].encode())
                            response = await client.send(request)
                            variants.append({"status": response.status_code, "response": response.text})
                    probes.append(
                        {
                            "request": {k: row[k] for k in ("method", "url", "body", "key")},
                            "body_sha256": hashlib.sha256(row["body"].encode()).hexdigest(),
                            "variants": variants,
                        }
                    )
                emit({"kind": "variance", "case_id": case["id"], "probes": probes})
        final = await snapshot(new)
        emit({"kind": "completed", "snapshot_unchanged": final == initial, "snapshot": final})
        assert final == initial, "Shared inputs changed during experiment"
    finally:
        await old.close()
        await new.close()


def main():
    manifest = json.load(sys.stdin)

    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    asyncio.run(evaluate(manifest, emit))


if __name__ == "__main__":
    main()
