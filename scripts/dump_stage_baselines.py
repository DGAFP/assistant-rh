"""Dump per-stage Python baselines for deterministic parity replay.

Example:

  uv run python scripts/dump_stage_baselines.py \
    --queries-file tests/conformance/queries.sample.jsonl \
    --output-dir tests/conformance/baselines/queries-sample
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from assistant_rh_rag_pipeline import Pipeline, create_pipeline
from assistant_rh_rag_pipeline.db_helpers import get_dsn
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts"

load_dotenv(REPO_ROOT / ".env")

STAGE_FILE_MAPPING: list[tuple[str, str | None]] = [
    ("00_input.json", None),
    ("01_query_processor.json", "query-processor"),
    ("02_retriever.json", "retriever"),
    ("03_section_aggregator.json", "section-aggregator"),
    ("04_context_selector.json", "context-selector"),
    ("05_context_builder.json", "context-builder"),
    ("06_generator.json", "generator"),
]


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    slug = slug.strip("-.")
    return slug or "query"


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
            query_id = str(obj.get("id") or f"q{len(queries) + 1}")
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
        WHERE {" AND ".join(where)}
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


def _git_commit_sha(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prompt_hashes() -> dict[str, str]:
    if not PROMPTS_DIR.exists():
        return {}

    hashes: dict[str, str] = {}
    for prompt_path in sorted(PROMPTS_DIR.glob("*.md")):
        if not prompt_path.is_file():
            continue
        hashes[prompt_path.name] = _hash_file(prompt_path)

    return hashes


def _write_query_stage_files(
    *,
    query_item: dict[str, Any],
    run_result: dict[str, Any],
    output_dir: Path,
) -> None:
    query_dir = output_dir / _safe_slug(str(query_item["id"]))

    stage_trace = run_result.get("stage_trace")
    if not isinstance(stage_trace, dict):
        stage_trace = {}
    stage_rows = stage_trace.get("stages")
    if not isinstance(stage_rows, dict):
        stage_rows = {}

    for filename, stage_name in STAGE_FILE_MAPPING:
        if stage_name is None:
            payload = {
                "id": query_item["id"],
                "query": query_item["query"],
                "conversation_history": query_item.get("conversation_history") or [],
                "tags": query_item.get("tags") or [],
            }
        else:
            payload = stage_rows.get(stage_name) or {"input": None, "output": None}

        _write_json(query_dir / filename, payload)

    _write_json(
        query_dir / "07_pipeline_result.json",
        {
            "answer": run_result.get("answer", ""),
            "timing": run_result.get("timing") or {},
            "sources": run_result.get("sources") or [],
            "metadata": run_result.get("metadata") or {},
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump Python stage baselines for conformance replay.")
    parser.add_argument("--queries-file", type=Path, help="JSONL file of queries.")
    parser.add_argument("--goldset-name", action="append", default=[], help="goldset_questions_v2.goldset_name filter (repeatable).")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of queries to run.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "tests/conformance/baselines/queries-sample",
        help="Directory where stage baseline files are written.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first failing query instead of continuing and recording errors.",
    )
    parser.add_argument(
        "--resolved-queries-out",
        type=Path,
        default=None,
        help="Optional JSONL output of the resolved query set used for this run.",
    )
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

    try:
        _ = get_dsn()
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SystemExit(f"Baseline dump requires DB DSN configuration: {exc}") from exc

    pipe: Pipeline = create_pipeline()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_queries_out: Path | None = None

    if args.resolved_queries_out:
        resolved_queries_out = args.resolved_queries_out
        if not resolved_queries_out.is_absolute():
            resolved_queries_out = (REPO_ROOT / resolved_queries_out).resolve()
        resolved_rows = [
            {
                "id": item["id"],
                "query": item["query"],
                "conversation_history": item.get("conversation_history") or [],
                "tags": item.get("tags") or [],
            }
            for item in queries
        ]
        _write_jsonl(resolved_queries_out, resolved_rows)

    errors: list[dict[str, str]] = []
    succeeded = 0

    for item in queries:
        query_id = str(item["id"])
        query = str(item["query"])
        history = item.get("conversation_history") or []

        try:
            result = pipe.run_with_trace(query, conversation_history=history)
            run_result = {
                "answer": result.answer,
                "timing": result.timing,
                "sources": result.sources,
                "metadata": result.metadata,
                "stage_trace": (result.metadata or {}).get("stage_trace", {}),
            }
            _write_query_stage_files(
                query_item=item,
                run_result=run_result,
                output_dir=output_dir,
            )
            succeeded += 1
        except Exception as exc:  # pragma: no cover - defensive path
            err = {"id": query_id, "query": query, "error": str(exc)}
            errors.append(err)
            if args.fail_fast:
                break

    manifest = {
        "generated_at": _now_iso(),
        "query_count": len(queries),
        "succeeded_count": succeeded,
        "failed_count": len(errors),
        "errors": errors,
        "git_commit_sha": _git_commit_sha(REPO_ROOT),
        "pipeline_config": pipe.config.to_dict(),
        "models": {
            "intent_model": pipe.config.query_processor.intent_model,
            "generation_model": pipe.config.generation.model,
            "generation_provider": pipe.config.generation.provider.value,
            "embedding_model": pipe.config.retrieval.embedding_model.value,
        },
        "prompt_hashes": _prompt_hashes(),
    }
    _write_json(output_dir / "manifest.json", manifest)

    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "query_count": len(queries),
                "succeeded": succeeded,
                "failed": len(errors),
                "manifest": str(output_dir / "manifest.json"),
                "resolved_queries": str(resolved_queries_out) if args.resolved_queries_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
