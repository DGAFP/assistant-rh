"""JSON contracts shared by the full-pipeline companion recorder and replay."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "tests/conformance/queries.m0-api-parity.jsonl"
BASELINE = ROOT / "tests/conformance/baselines/m0-api-parity-dev-9bf1cf0"


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(child) for child in value]
    if isinstance(value, (date, datetime, UUID)):
        return str(value)
    return value


def canonical(value):
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def dump(path, value):
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def assert_equal(actual, expected, label):
    """JSON types, array order and full float precision are part of equality."""
    if canonical(actual) != canonical(expected):
        raise AssertionError(f"{label}: exact JSON differs at {difference(plain(actual), plain(expected))}")


def difference(actual, expected, path="$"):
    if type(actual) is not type(expected):
        return path + f" (types {type(actual).__name__}/{type(expected).__name__})"
    if isinstance(actual, dict):
        if actual.keys() != expected.keys():
            return path + " (keys)"
        for key in actual:
            if canonical(actual[key]) != canonical(expected[key]):
                return difference(actual[key], expected[key], path + "." + key)
    if isinstance(actual, list):
        if len(actual) != len(expected):
            return path + f" (lengths {len(actual)}/{len(expected)})"
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            if canonical(left) != canonical(right):
                return difference(left, right, path + f"[{index}]")
    return path


def query_output(result):
    value = plain(result)
    if isinstance(value["detected_acronyms"], list):
        value["detected_acronyms"] = {item["short"]: item["expansion"] for item in value["detected_acronyms"]}
    return value


def aggregation_output(result):
    diagnostics = result.diagnostics
    return {
        "sections": plain(result.sections),
        "diagnostics": {
            name: plain(getattr(diagnostics, name))
            for name in (
                "sections_before_rerank",
                "sections_after_rerank",
                "reranker_status",
                "reranker_error",
                "chunks_before_rerank",
                "chunks_after_rerank",
            )
        },
    }


def selection_output(sections, *, decisions, raw_response, reason, all_rejected, prompt_chars):
    return plain(
        dict(sections=sections, decisions=decisions, raw_response=raw_response, reason=reason, all_rejected=all_rejected, prompt_chars=prompt_chars)
    )


def context_output(items, refs, formatted):
    return plain(dict(items=items, resolved_refs=refs, formatted_context=formatted))


def legacy_sources(items):
    result, seen = [], set()
    for item in items:
        key = (item.section_id or "", item.heading)
        if key not in seen:
            seen.add(key)
            result.append({name: getattr(item, name) for name in ("heading", "publisher", "document_title", "document_url", "score")})
    return plain(result)


def final_output(answer, items, sources, diagnostics):
    return plain(
        {
            "answer": answer,
            "context_items": items,
            "sources": sources,
            "selector_all_rejected": diagnostics.get("selector_all_rejected", False),
            "selector_retry_triggered": diagnostics.get("selector_retry_triggered", False),
            "selector_retry_succeeded": diagnostics.get("selector_retry_succeeded", False),
        }
    )


class Tape:
    """Match calls by their complete arguments; never by an expected stage result."""

    def __init__(self, records):
        self.pending = defaultdict(deque)
        self.count = 0
        for row in records:
            self.pending[canonical([row["operation"], row["request"]])].append(row["response"])

    def take(self, operation, request):
        rows = self.pending[canonical([operation, request])]
        if not rows:
            raise AssertionError(f"Unrecorded or repeated port call: {operation}")
        self.count += 1
        return rows.popleft()

    def assert_consumed(self):
        if any(self.pending.values()):
            raise AssertionError("Recorded port calls were not consumed")


def invocation(args, kwargs, operation=""):
    value = plain({"args": args, "kwargs": kwargs})
    if operation in ("content.sections", "content.references"):
        # Batched lookups are sets of IDs; response order remains recorded.
        value["args"][0] = sorted(set(value["args"][0]))
    return value


class RecordedStore:
    def __init__(self, prefix, store, records):
        self.prefix, self.store, self.records = prefix, store, records

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            request = invocation(args, kwargs, f"{self.prefix}.{name}")
            result = await getattr(self.store, name)(*args, **kwargs)
            self.records.append({"operation": f"{self.prefix}.{name}", "request": request, "response": plain(result)})
            return result

        return call


class ReplayStore:
    def __init__(self, prefix, tape):
        self.prefix, self.tape = prefix, tape

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            operation = f"{self.prefix}.{name}"
            value = self.tape.take(operation, invocation(args, kwargs, operation))
            return decode(operation, value)

        return call


def decode(operation, value):
    from assistant_rh_api.core.models.configuration import Acronym, Prompt, Snapshot
    from assistant_rh_api.core.models.retrieval import Document, LegalReference, RawChunk, Section, freeze_metadata

    if value is None:
        return None
    if operation == "config.load":
        return Snapshot(freeze_metadata(value["value"]), value["revision"], value["origin"])
    if operation in ("prompts.get", "packaged.get"):
        return Snapshot(Prompt(**value["value"]), value["revision"], value["origin"])
    if operation == "acronyms.load":
        return Snapshot(tuple(Acronym(**row) for row in value["value"]), value["revision"], value["origin"])
    if operation == "search.search":
        return tuple(RawChunk(**row) for row in value)
    if operation == "search.hybrid_candidates":
        return tuple(tuple(RawChunk(**row) for row in lane) for lane in value)
    if operation == "content.sections":
        return tuple(Section(**{**row, "document": Document(**row["document"]) if row["document"] else None}) for row in value)
    if operation == "content.documents":
        return tuple(Document(**row) for row in value)
    if operation == "content.references":
        return tuple(LegalReference(**row) for row in value)
    raise AssertionError(f"Unknown port result type: {operation}")
