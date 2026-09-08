"""Canonical content revisions and detached immutable JSON snapshots."""

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType

from assistant_rh_api.core.models import JsonValue


def freeze_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        # Reject non-finite floats, which have no portable JSON representation.
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in sorted(value.items())})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError("Unsupported JSON value")


def content_revision(value: JsonValue) -> str:
    """Equal JSON content has the same revision regardless of object key order.

    Array order and exact text are significant. This is a cache equality token,
    not a monotonic version, a secret hash, or a replacement for DB write locking.
    """
    payload = json.dumps(value, default=_json_default, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Unsupported JSON value")
