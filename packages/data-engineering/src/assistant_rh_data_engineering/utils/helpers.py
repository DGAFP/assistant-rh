from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SERVICE_PUBLIC_NAMESPACE = uuid.UUID("11111111-2222-3333-4444-555555555555")
LEGIFRANCE_NAMESPACE = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_uuid_from_parts(namespace: uuid.UUID, *parts: object) -> str:
    key = ":".join(str(part) for part in parts)
    return str(uuid.uuid5(namespace, key))


def stable_doc_uuid(short_id: str, source_url: str) -> str:
    return stable_uuid_from_parts(
        SERVICE_PUBLIC_NAMESPACE,
        "service_public",
        short_id,
        source_url,
    )


def stable_section_uuid(doc_id: str, section_index: int) -> str:
    return stable_uuid_from_parts(
        SERVICE_PUBLIC_NAMESPACE,
        doc_id,
        "section",
        section_index,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json_dumps(data), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def vector_to_pgvector(value: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in value) + "]"
