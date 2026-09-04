"""Run conformance checks between the Python pipeline and an API candidate.

Usage examples:

  # 1) Generate Python baseline snapshots only
  uv run python scripts/run_candidate_conformance.py \
    --queries-file tests/conformance/queries.sample.jsonl \
    --output tests/conformance/reports/python_baseline.json

  # 2) Compare against an OpenAI-compatible candidate endpoint
  uv run python scripts/run_candidate_conformance.py \
    --queries-file tests/conformance/queries.sample.jsonl \
    --candidate-base-url http://localhost:4111 \
    --candidate-model assistant-rh \
    --output tests/conformance/reports/python_vs_candidate.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import requests
from assistant_rh_rag_pipeline import Pipeline, create_pipeline
from assistant_rh_rag_pipeline.conformance import aggregate_conformance, compare_query_runs
from assistant_rh_rag_pipeline.db_helpers import get_dsn
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(REPO_ROOT / ".env")


DEFAULT_THRESHOLDS: dict[str, float] = {
    "intent_match_rate": 0.95,
    "retrieval_overlap_topk_avg": 0.80,
    "section_overlap_topk_avg": 0.80,
    "context_overlap_topk_avg": 0.75,
    "answer_token_jaccard_avg": 0.70,
    "latency_ratio_p95": 1.30,
}


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _parse_queries_file(path: Path) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            obj = json.loads(stripped)
            query = str(obj.get("query", "") or "").strip()
            if not query:
                raise ValueError(f"Missing 'query' at {path}:{idx}")
            query_id = str(obj.get("id") or f"q{len(queries)+1}")
            queries.append(
                {
                    "id": query_id,
                    "query": query,
                    "conversation_history": obj.get("conversation_history") or [],
                    "tags": obj.get("tags") or [],
                }
            )
    return queries


def _load_queries_from_goldset(goldset_names: list[str], limit: int | None) -> list[dict[str, Any]]:
    where = ["gold_sources IS NOT NULL", "gold_sources != ''"]
    params: list[Any] = []
    if goldset_names:
        placeholders = ",".join(["%s"] * len(goldset_names))
        where.append(f"goldset_name IN ({placeholders})")
        params.extend(goldset_names)

    limit_sql = ""
    if limit:
        limit_sql = " LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT id, question, goldset_name, tags
        FROM goldset_questions_v2
        WHERE {' AND '.join(where)}
        ORDER BY id
        {limit_sql}
    """

    dsn = get_dsn()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {
            "id": f"goldset-{row['id']}",
            "query": row["question"],
            "conversation_history": [],
            "tags": (row.get("tags") or []) + ([row.get("goldset_name")] if row.get("goldset_name") else []),
        }
        for row in rows
    ]


def _build_messages(query: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for msg in history:
        role = str(msg.get("role", "") or "").strip()
        content = str(msg.get("content", "") or "")
        if role and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})
    return messages


def run_python_pipeline_case(pipe: Pipeline, query: str, conversation_history: list[dict[str, str]]) -> dict[str, Any]:
    started = time.perf_counter()
    result = pipe.run(query, conversation_history=conversation_history)
    elapsed_ms = (time.perf_counter() - started) * 1000
    timing = dict(result.timing)
    timing.setdefault("pipeline_total_ms", elapsed_ms)
    return {
        "answer": result.answer,
        "timing": timing,
        "sources": result.sources,
        "metadata": result.metadata,
    }


def run_candidate_case(
    *,
    base_url: str,
    model: str,
    query: str,
    conversation_history: list[dict[str, str]],
    api_key: str | None,
    temperature: float,
    timeout_s: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": _build_messages(query, conversation_history),
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()

    body = response.json()
    answer = ""
    try:
        answer = str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        answer = ""

    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    # Optional compatibility path: if implementation returns stage refs under
    # choices[0].message.metadata we also ingest it.
    nested_metadata: dict[str, Any] = {}
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                message_metadata = message.get("metadata")
                if isinstance(message_metadata, dict):
                    nested_metadata = message_metadata

    if nested_metadata:
        metadata = {**metadata, **nested_metadata}

    return {
        "answer": answer,
        "timing": {"pipeline_total_ms": elapsed_ms},
        "usage": body.get("usage"),
        "metadata": metadata,
        "raw": body,
    }


def _load_thresholds(path: Path | None) -> dict[str, float]:
    if path is None:
        return dict(DEFAULT_THRESHOLDS)
    raw = json.loads(path.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in raw.items():
        merged[key] = float(value)
    return merged


def _check_thresholds(summary: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    failures: list[str] = []
    for metric, expected in thresholds.items():
        actual = summary.get(metric)
        if actual is None:
            continue

        if metric.startswith("latency_ratio"):
            if actual > expected:
                failures.append(f"{metric}: {actual:.4f} > {expected:.4f}")
        else:
            if actual < expected:
                failures.append(f"{metric}: {actual:.4f} < {expected:.4f}")
    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Conformance runner for the Python pipeline versus an API candidate.")
    parser.add_argument("--queries-file", type=Path, help="JSONL file of queries.")
    parser.add_argument("--goldset-name", action="append", default=[], help="goldset_questions_v2.goldset_name filter (repeatable).")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of queries to run.")
    parser.add_argument("--output", type=Path, required=True, help="Path to output report JSON.")
    parser.add_argument("--top-k", type=int, default=10, help="Top-k for overlap metrics.")
    parser.add_argument("--candidate-base-url", type=str, default=None, help="OpenAI-compatible candidate base URL.")
    parser.add_argument("--candidate-model", type=str, default="assistant-rh", help="Model name for /chat/completions calls.")
    parser.add_argument("--candidate-api-key", type=str, default=None, help="API key for candidate endpoint. Defaults to OPENAI_API_KEY.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Candidate generation temperature.")
    parser.add_argument("--timeout-s", type=int, default=120, help="Candidate HTTP timeout.")
    parser.add_argument("--thresholds-file", type=Path, default=None, help="JSON file overriding default thresholds.")
    parser.add_argument("--enforce-thresholds", action="store_true", help="Exit non-zero if threshold checks fail.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.queries_file and not args.goldset_name:
        raise SystemExit("Provide --queries-file or at least one --goldset-name")

    queries: list[dict[str, Any]] = []
    if args.queries_file:
        queries.extend(_parse_queries_file(args.queries_file))
    if args.goldset_name:
        queries.extend(_load_queries_from_goldset(args.goldset_name, args.limit))

    if args.limit:
        queries = queries[: args.limit]

    if not queries:
        raise SystemExit("No queries loaded")

    # Preflight: baseline Python run always needs DB access.
    try:
        _ = get_dsn()
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SystemExit(f"Python baseline requires DB DSN configuration: {exc}") from exc

    candidate_enabled = bool(args.candidate_base_url)
    candidate_api_key = args.candidate_api_key or os.getenv("OPENAI_API_KEY")
    pipe = create_pipeline()

    report: dict[str, Any] = {
        "generated_at": _now_iso(),
        "mode": "python_vs_candidate" if candidate_enabled else "python_only",
        "query_count": len(queries),
        "top_k": args.top_k,
        "candidate": {
            "base_url": args.candidate_base_url,
            "model": args.candidate_model,
            "temperature": args.temperature,
        },
        "results": [],
        "summary": {},
    }

    conformance_rows = []
    failures: list[dict[str, Any]] = []

    for item in queries:
        qid = str(item["id"])
        query = str(item["query"])
        history = item.get("conversation_history") or []

        row: dict[str, Any] = {
            "id": qid,
            "query": query,
            "tags": item.get("tags") or [],
            "python": None,
            "candidate": None,
            "comparison": None,
            "error": None,
        }

        try:
            py_run = run_python_pipeline_case(pipe, query, history)
            row["python"] = py_run

            if candidate_enabled:
                ca_run = run_candidate_case(
                    base_url=args.candidate_base_url,
                    model=args.candidate_model,
                    query=query,
                    conversation_history=history,
                    api_key=candidate_api_key,
                    temperature=args.temperature,
                    timeout_s=args.timeout_s,
                )
                row["candidate"] = ca_run
                cmp_row = compare_query_runs(query_id=qid, python_run=py_run, candidate_run=ca_run, top_k=args.top_k)
                row["comparison"] = asdict(cmp_row)
                conformance_rows.append(cmp_row)

        except Exception as exc:  # pragma: no cover - defensive path
            row["error"] = str(exc)
            failures.append({"id": qid, "error": str(exc)})

        report["results"].append(row)

    if conformance_rows:
        report["summary"] = aggregate_conformance(conformance_rows)
    report["errors"] = failures

    thresholds = _load_thresholds(args.thresholds_file)
    report["thresholds"] = thresholds
    threshold_failures: list[str] = []
    if candidate_enabled and report["summary"]:
        threshold_failures = _check_thresholds(report["summary"], thresholds)
    report["threshold_failures"] = threshold_failures

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(args.output),
                "mode": report["mode"],
                "query_count": len(queries),
                "errors": len(failures),
                "threshold_failures": len(threshold_failures),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if failures:
        return 1
    if args.enforce_thresholds and threshold_failures:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
