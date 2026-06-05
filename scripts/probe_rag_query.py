"""Run a local RAG query probe and print the main diagnostic stages.

Example:

  uv run python scripts/probe_rag_query.py "Quelles sont les conditions pour recevoir le SFT ?"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from assistant_rh_rag_pipeline import create_pipeline
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def _json_default(value: Any) -> str:
    return str(value)


def _print_json(result: Any) -> None:
    payload = {
        "query": result.query,
        "answer": result.answer,
        "timing": result.timing,
        "sources": result.sources,
        "metadata": result.metadata,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _print_items(title: str, rows: list[dict[str, Any]], *, limit: int) -> None:
    print(f"\n== {title} ==")
    if not rows:
        print("(none)")
        return

    for idx, row in enumerate(rows[:limit], start=1):
        label = row.get("heading") or row.get("chunk_id") or row.get("section_id") or f"item {idx}"
        score = row.get("score")
        score_text = f" score={score}" if score is not None else ""
        publisher = row.get("publisher") or row.get("table") or row.get("source_table") or ""
        publisher_text = f" source={publisher}" if publisher else ""
        print(f"{idx}. {label}{score_text}{publisher_text}")

        preview = row.get("preview")
        if preview:
            print(f"   {preview}")


def _print_text(result: Any, *, limit: int) -> None:
    diagnostics = result.metadata.get("rag_diagnostics") or {}
    query = diagnostics.get("query") or {}
    retrieval = diagnostics.get("retrieval") or {}
    aggregation = diagnostics.get("aggregation") or {}
    reranker = diagnostics.get("reranker") or {}
    selector = diagnostics.get("selector") or {}

    print("== RAG probe ==")
    print(f"Original query: {query.get('original') or result.query}")
    print(f"Enriched query: {query.get('enriched') or ''}")
    print(f"Retrieval query: {query.get('retrieval') or ''}")
    print(f"Tables searched: {', '.join(retrieval.get('tables_searched') or []) or '(none)'}")

    _print_items("Retrieved chunks", retrieval.get("retrieved_chunks") or [], limit=limit)

    print("\n== Aggregation ==")
    print(f"Sections before rerank: {aggregation.get('sections_before_rerank', 0)}")
    print(f"Sections after rerank: {aggregation.get('sections_after_rerank', 0)}")
    section_reranker = (reranker.get("section") or {}) if isinstance(reranker, dict) else {}
    print(f"Section reranker: {section_reranker.get('status', 'unknown')}")
    _print_items("Aggregated sections", aggregation.get("aggregated_sections") or [], limit=limit)

    print("\n== Selector ==")
    print(f"Enabled: {selector.get('enabled', False)}")
    print(f"Decision: {selector.get('decision', 'not_run')}")
    print(f"All rejected: {selector.get('all_rejected', False)}")
    reason = selector.get("rejection_reason") or selector.get("reason") or ""
    if reason:
        print(f"Reason: {reason}")

    print("\n== Answer ==")
    print(result.answer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay one RAG question and print diagnostic stages.")
    parser.add_argument("query", help="Question to send to the RAG pipeline.")
    parser.add_argument("--json", action="store_true", help="Print the full result payload as JSON.")
    parser.add_argument("--limit", type=int, default=8, help="Maximum chunks/sections to display per stage.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    result = create_pipeline().run_with_trace(args.query)

    if args.json:
        _print_json(result)
    else:
        _print_text(result, limit=max(args.limit, 0))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
