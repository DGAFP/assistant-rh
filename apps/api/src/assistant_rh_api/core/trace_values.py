"""Bounded JSON traces; pipeline inputs and outputs remain untouched."""

import json
import re
from collections.abc import Mapping
from itertools import islice

from assistant_rh_api.core.models.configuration import JsonValue
from assistant_rh_api.core.models.context import freeze_value
from assistant_rh_api.core.models.inference import Attempt
from assistant_rh_api.core.sources import public_source_url

MAX_TRACE_BYTES = 65_536
MAX_TRACE_TEXT = 4_096
MAX_TRACE_ITEMS = 40
MAX_TRACE_DEPTH = 12
TRUNCATED = "[truncated]"
PRIVATE_URL = "[private URL]"
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def attempt_trace(attempts: tuple[Attempt, ...]) -> tuple[JsonValue, ...]:
    return tuple({"provider": a.provider, "model": a.model, "error": a.error, "status": a.status} for a in attempts)


def _clean(value: JsonValue, depth: int = 0):
    if depth >= MAX_TRACE_DEPTH:
        return TRUNCATED
    if isinstance(value, str):
        safe = _URL.sub(lambda match: public_source_url(match.group()) or PRIVATE_URL, value)
        return safe if len(safe) <= MAX_TRACE_TEXT else safe[:MAX_TRACE_TEXT] + TRUNCATED
    if isinstance(value, Mapping):
        cleaned_map = {_clean(key, depth + 1): _clean(child, depth + 1) for key, child in islice(value.items(), MAX_TRACE_ITEMS)}
        if len(value) > MAX_TRACE_ITEMS:
            cleaned_map["__trace_truncated__"] = True
        return cleaned_map
    if isinstance(value, (tuple, list)):
        cleaned_items = [_clean(child, depth + 1) for child in value[:MAX_TRACE_ITEMS]]
        if len(value) > MAX_TRACE_ITEMS:
            cleaned_items.append(TRUNCATED)
        return cleaned_items
    return value


def trace_payload(value: JsonValue) -> JsonValue:
    """Redact before truncating, including URLs embedded in prompts or Markdown."""
    cleaned = _clean(value)
    size = len(json.dumps(cleaned, ensure_ascii=True, allow_nan=False).encode("utf-8"))
    if size > MAX_TRACE_BYTES:
        cleaned = {"truncated": True, "reason": "trace_size_limit", "size_bytes": size}
    return freeze_value(cleaned)
