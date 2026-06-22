"""Build query-processor replay cache from stage baseline artifacts.

Example:

  uv run python scripts/build_query_processor_replay_cache.py \
    --baseline-dir tests/conformance/baselines/queries-sample \
    --output tests/conformance/replay-cache/query-processor.intent.v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _stable_json(value: Any) -> str:
    """Match llm-replay.ts stable JSON behavior for key hashing."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class ReplayEntry:
    key: str
    stage: str
    request_digest: str
    response: str
    created_at: str
    metadata: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "stage": self.stage,
            "requestDigest": self.request_digest,
            "response": self.response,
            "createdAt": self.created_at,
            "metadata": self.metadata,
        }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _llm_needs_legal(output: dict[str, Any]) -> Any:
    """Replay ground-truth for needs_legal_search.

    Use the LLM-only signal so the Mastra side (which has no Python heuristic)
    compares like-for-like; fall back to the merged value when the LLM signal
    isn't recorded, preserving pre-fix behavior for older baselines.
    """
    llm = output.get("needs_legal_search_llm")
    return llm if llm is not None else output.get("needs_legal_search")


def _build_response_payload(stage: dict[str, Any]) -> dict[str, Any]:
    output = stage.get("output") or {}
    query_for_retrieval = output.get("query_for_retrieval")

    return {
        "intent": output.get("intent"),
        "theme": output.get("theme"),
        "needs_legal_search": _llm_needs_legal(output),
        "reformulated_query": output.get("reformulated_query"),
        "query_for_retrieval": query_for_retrieval,
        "confidence": output.get("confidence", 1),
        "reasoning": output.get("reasoning", "Deterministic replay cache response from baseline"),
    }


def _iter_fixture_dirs(baseline_dir: Path) -> list[Path]:
    return sorted(path for path in baseline_dir.iterdir() if path.is_dir())


def _load_existing_created_at(output: Path) -> dict[str, str]:
    if not output.exists():
        return {}

    try:
        raw = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}

    out: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        created_at = entry.get("createdAt")
        if isinstance(key, str) and isinstance(created_at, str):
            out[key] = created_at
    return out


def build_cache(*, baseline_dir: Path, created_at: str, existing_created_at: dict[str, str]) -> dict[str, Any]:
    entries: list[ReplayEntry] = []

    for fixture_dir in _iter_fixture_dirs(baseline_dir):
        fixture_id = fixture_dir.name
        input_path = fixture_dir / "00_input.json"
        query_processor_path = fixture_dir / "01_query_processor.json"

        if not input_path.exists() or not query_processor_path.exists():
            continue

        input_stage = _load_json(input_path)
        query_processor_stage = _load_json(query_processor_path)

        query = str(input_stage.get("query") or "").strip()
        conversation_history = input_stage.get("conversation_history") or []

        payload = {
            "fixtureId": fixture_id,
            "query": query,
            "conversationHistory": conversation_history,
        }
        request_digest = _sha256_hex(_stable_json(payload))
        key = f"query-processor.intent:{request_digest}"

        response_payload = _build_response_payload(query_processor_stage)
        response = json.dumps(response_payload, ensure_ascii=False)

        output = (query_processor_stage.get("output") or {}) if isinstance(query_processor_stage, dict) else {}
        entry = ReplayEntry(
            key=key,
            stage="query-processor.intent",
            request_digest=request_digest,
            response=response,
            created_at=existing_created_at.get(key, created_at),
            metadata={
                "fixtureId": fixture_id,
                "source": "baseline-01_query_processor",
                "intent": output.get("intent"),
                "theme": output.get("theme"),
                # LLM-only value when available, post-heuristic otherwise.
                "needsLegalSearch": _llm_needs_legal(output),
                # Always preserve the merged value separately for analytics.
                "needsLegalSearchMerged": output.get("needs_legal_search"),
            },
        )
        entries.append(entry)

    entries.sort(key=lambda item: item.key)

    return {
        "version": 1,
        "entries": [entry.to_dict() for entry in entries],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build query-processor replay cache JSON from baseline stage artifacts.",
    )
    parser.add_argument(
        "--baseline-dir",
        type=Path,
        default=REPO_ROOT / "tests/conformance/baselines/queries-sample",
        help="Directory containing per-fixture baseline stage folders.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "tests/conformance/replay-cache/query-processor.intent.v1.json",
        help="Target replay cache file path.",
    )
    parser.add_argument(
        "--created-at",
        type=str,
        default=None,
        help="Override createdAt timestamp for all entries (ISO). Defaults to current UTC time.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    baseline_dir = args.baseline_dir
    if not baseline_dir.is_absolute():
        baseline_dir = (REPO_ROOT / baseline_dir).resolve()

    output = args.output
    if not output.is_absolute():
        output = (REPO_ROOT / output).resolve()

    created_at = args.created_at or datetime.now(tz=UTC).isoformat()

    if not baseline_dir.exists():
        raise SystemExit(f"Baseline directory not found: {baseline_dir}")

    existing_created_at = _load_existing_created_at(output)
    payload = build_cache(
        baseline_dir=baseline_dir,
        created_at=created_at,
        existing_created_at=existing_created_at,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "baseline_dir": str(baseline_dir),
                "output": str(output),
                "entry_count": len(payload.get("entries") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
