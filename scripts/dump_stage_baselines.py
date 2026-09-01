"""Dump per-stage Python baselines for deterministic parity replay.

Example:

  uv run python scripts/dump_stage_baselines.py \
    --queries-file tests/conformance/queries.sample.jsonl \
    --output-dir tests/conformance/baselines/queries-sample \
    --runtime-git-sha "$RUNTIME_GIT_SHA"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from assistant_rh_rag_pipeline import Pipeline, create_pipeline
from assistant_rh_rag_pipeline.admin import get_rag_config, runtime_config_to_rag_config
from assistant_rh_rag_pipeline.db_helpers import get_dsn
from assistant_rh_rag_pipeline.ministry_scope import build_retrieval_scope, known_ministry_ids
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    from scripts.parity_revision import assert_runtime_revision_compatible, get_git_sha
except ModuleNotFoundError:  # Direct execution: python scripts/dump_stage_baselines.py
    from parity_revision import assert_runtime_revision_compatible, get_git_sha

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

REPLAY_SCHEMA_VERSION = "m0-replay-v1"
PIPELINE_METADATA_KEYS = (
    "original_query",
    "intent",
    "intent_confidence",
    "theme",
    "was_expanded",
    "expanded_acronyms",
    "enriched_query",
    "query_for_retrieval",
    "needs_legal_search",
    "needs_legal_search_llm",
    "tables_searched",
    "retrieval_scope",
    "selected_ministry",
    "scoped_table_keys",
    "selector_enabled",
    "generator_model",
    "generator_provider",
    "generator_provider_used",
    "generator_fallback_count",
    "generator_used_fallback",
    "generator_model_used",
    "embedding_model",
    "retrieved_chunks",
    "aggregated_sections",
    "context_items_ref",
    "selector_decisions",
    "selector_reasoning",
    "selector_rejection_reason",
    "selector_items_before",
    "selector_items_after",
    "sections_before_rerank",
    "sections_after_rerank",
    "selector_all_rejected",
    "selector_retry_triggered",
    "selector_retry_succeeded",
    "selector_decision",
    "reranker_status",
)

PERSONAL_DATA_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "french_phone": re.compile(r"(?<!\d)(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}(?!\d)"),
    "nir": re.compile(r"(?<!\d)[12]\s?\d{2}\s?(?:0[1-9]|1[0-2])\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}(?!\d)"),
}


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
            ministry = str(obj.get("ministry") or "").strip().lower()
            if ministry and ministry not in known_ministry_ids():
                raise ValueError(f"Unknown ministry {ministry!r} at {path}:{idx}")
            item = {
                "id": query_id,
                "query": query,
                "conversation_history": obj.get("conversation_history") or [],
                "tags": obj.get("tags") or [],
                "ministry": ministry or None,
                "expected": obj.get("expected") or {},
            }
            _assert_no_personal_data(item)
            queries.append(item)
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

    queries = []
    for row in rows:
        item = {
            "id": f"goldset-{row['id']}",
            "query": row["question"],
            "conversation_history": [],
            "tags": (row.get("tags") or []) + ([row.get("goldset_name")] if row.get("goldset_name") else []),
            "ministry": None,
            "expected": {},
        }
        _assert_no_personal_data(item)
        queries.append(item)
    return queries


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assert_no_personal_data(query_item: dict[str, Any]) -> None:
    values = [str(query_item.get("query") or "")]
    for message in query_item.get("conversation_history") or []:
        if isinstance(message, dict):
            values.append(str(message.get("content") or ""))
    text = "\n".join(values)
    matches = [name for name, pattern in PERSONAL_DATA_PATTERNS.items() if pattern.search(text)]
    if matches:
        raise ValueError(f"Fixture {query_item['id']!r} contains possible personal data: {', '.join(matches)}")


def _stable_pipeline_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: metadata[key] for key in PIPELINE_METADATA_KEYS if key in metadata}


def _stable_stage_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    stable = dict(payload)
    stage_input = stable.get("input")
    if isinstance(stage_input, dict):
        stable["input"] = {key: value for key, value in stage_input.items() if key != "attempts"}
    stage_output = stable.get("output")
    if isinstance(stage_output, dict):
        stable["output"] = {key: value for key, value in stage_output.items() if key != "attempts"}
    return stable


def _prompt_hashes() -> dict[str, str]:
    if not PROMPTS_DIR.exists():
        return {}

    hashes: dict[str, str] = {}
    for prompt_path in sorted(PROMPTS_DIR.glob("*.md")):
        if not prompt_path.is_file():
            continue
        hashes[prompt_path.name] = _hash_file(prompt_path)

    return hashes


def _database_prompt_hashes() -> dict[str, dict[str, Any]]:
    dsn = get_dsn()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT name, prompt_type, content, updated_at
            FROM system_prompts
            WHERE is_active IS TRUE
            ORDER BY name
            """
        ).fetchall()
    return {
        str(row["name"]): {
            "prompt_type": row["prompt_type"],
            "sha256": hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest(),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    }


def _reference_run_snapshot(run_id: int) -> dict[str, Any]:
    dsn = get_dsn()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT id, status, run_label, git_sha, config_fingerprint, config, metadata
            FROM rag_quality_eval_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"Reference eval run {run_id} does not exist")
    if row.get("status") != "completed":
        raise RuntimeError(f"Reference eval run {run_id} is {row.get('status')!r}, not completed")
    config = dict(row.get("config") or {})
    metadata = dict(row.get("metadata") or {})
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "run_label": row["run_label"],
        "git_sha": row["git_sha"],
        "config_fingerprint": row["config_fingerprint"],
        "pipeline_config_fingerprint": _json_sha256(config),
        "generator_system_prompt_sha": metadata.get("generator_system_prompt_sha"),
        "eval_scope": metadata.get("eval_scope") or {},
    }


def _prepare_output_dir(output_dir: Path, *, replace: bool) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"Replay output path is not a directory: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        if not replace:
            raise SystemExit(
                f"Replay output directory is not empty: {output_dir}. "
                "Choose an empty directory or pass --replace-output-dir for an existing replay bundle."
            )
        resolved_output_dir = output_dir.resolve()
        protected_paths = {
            Path("/").resolve(),
            Path("/tmp").resolve(),
            Path.home().resolve(),
            REPO_ROOT.resolve(),
            REPO_ROOT.parent.resolve(),
        }
        if resolved_output_dir in protected_paths:
            raise SystemExit(f"Refusing to replace protected directory: {output_dir}")
        manifest_path = output_dir / "manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(f"Refusing to replace nonempty directory without a replay manifest: {output_dir}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Refusing to replace directory with an invalid replay manifest: {output_dir}") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("query_count"), int):
            raise SystemExit(f"Refusing to replace directory with an invalid replay manifest: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


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
                "ministry": query_item.get("ministry"),
                "expected": query_item.get("expected") or {},
            }
        else:
            payload = _stable_stage_payload(stage_rows.get(stage_name) or {"input": None, "output": None})

        _write_json(query_dir / filename, payload)

    _write_json(
        query_dir / "07_pipeline_result.json",
        {
            "query": query_item["query"],
            "answer": run_result.get("answer", ""),
            "sources": run_result.get("sources") or [],
            "metadata": _stable_pipeline_metadata(run_result.get("metadata") or {}),
        },
    )


def _observed_coverage(query_item: dict[str, Any], run_result: dict[str, Any]) -> dict[str, Any]:
    metadata = run_result.get("metadata") or {}
    stage_trace = run_result.get("stage_trace") or {}
    stages = stage_trace.get("stages") or {}
    query_output = (stages.get("query-processor") or {}).get("output") or {}
    return {
        "id": query_item["id"],
        "expected": query_item.get("expected") or {},
        "observed": {
            "intent": query_output.get("intent"),
            "should_proceed": query_output.get("should_proceed"),
            "needs_legal_search": query_output.get("needs_legal_search"),
            "selector_decision": metadata.get("selector_decision", "not_run"),
            "selector_retry_triggered": bool(metadata.get("selector_retry_triggered", False)),
            "selector_retry_succeeded": bool(metadata.get("selector_retry_succeeded", False)),
            "selected_ministry": metadata.get("selected_ministry"),
            "tables_searched": metadata.get("tables_searched") or [],
            "generator_used_fallback": bool(metadata.get("generator_used_fallback", False)),
        },
    }


def _validate_expected_coverage(coverage: dict[str, Any]) -> list[str]:
    expected = coverage.get("expected") or {}
    observed = coverage.get("observed") or {}
    errors = []
    for key, expected_value in expected.items():
        if observed.get(key) != expected_value:
            errors.append(f"{coverage['id']}: expected {key}={expected_value!r}, got {observed.get(key)!r}")
    return errors


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
    parser.add_argument(
        "--runtime-git-sha",
        "--expected-git-sha",
        dest="runtime_git_sha",
        default="",
        help=(
            "Git revision of the runtime being recorded. This may differ from the recorder checkout; "
            "--expected-git-sha is retained as a compatibility alias."
        ),
    )
    parser.add_argument("--reference-run-id", type=int, default=None, help="Recorded live eval run linked to this replay bundle.")
    parser.add_argument("--source-environment", default="scaleway-staging", help="Non-secret label for the live recording source.")
    parser.add_argument(
        "--replace-output-dir",
        action="store_true",
        help="Replace a nonempty output directory only when it already contains a replay manifest.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        recorder_git_sha = get_git_sha(REPO_ROOT)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Cannot determine the recorder checkout Git revision: {exc}") from exc
    runtime_git_sha = args.runtime_git_sha or recorder_git_sha
    try:
        assert_runtime_revision_compatible(REPO_ROOT, runtime_git_sha, recorder_git_sha)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

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
        dsn = get_dsn()
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SystemExit(f"Baseline dump requires DB DSN configuration: {exc}") from exc

    # Record the exact runtime configuration used by Streamlit and the live
    # evaluation runner. ``create_pipeline()`` alone would silently use the
    # package defaults, which are not a valid parity reference for staging.
    pipeline_config = runtime_config_to_rag_config(get_rag_config())
    pipe: Pipeline = create_pipeline(config=pipeline_config, dsn=dsn)
    normalized_pipeline_config = pipe.config.to_dict()
    reference_run = _reference_run_snapshot(args.reference_run_id) if args.reference_run_id is not None else None
    if reference_run is not None:
        if reference_run["git_sha"] != runtime_git_sha:
            raise RuntimeError(
                f"Reference run Git SHA {reference_run['git_sha']} does not match declared runtime {runtime_git_sha}"
            )
        if reference_run["pipeline_config_fingerprint"] != _json_sha256(normalized_pipeline_config):
            raise RuntimeError("Reference run pipeline configuration does not match the normalized runtime configuration")

    output_dir: Path = args.output_dir
    _prepare_output_dir(output_dir, replace=args.replace_output_dir)

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
                "ministry": item.get("ministry"),
                "expected": item.get("expected") or {},
            }
            for item in queries
        ]
        _write_jsonl(resolved_queries_out, resolved_rows)

    errors: list[dict[str, str]] = []
    coverage: list[dict[str, Any]] = []
    succeeded = 0

    for item in queries:
        query_id = str(item["id"])
        query = str(item["query"])
        history = item.get("conversation_history") or []
        ministry = item.get("ministry")
        retrieval_scope = build_retrieval_scope(ministry) if ministry else None

        try:
            result = pipe.run_with_trace(query, conversation_history=history, retrieval_scope=retrieval_scope)
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
            coverage.append(_observed_coverage(item, run_result))
            succeeded += 1
        except Exception as exc:  # pragma: no cover - defensive path
            err = {"id": query_id, "query": query, "error": str(exc)}
            errors.append(err)
            if args.fail_fast:
                break

    coverage_errors = [error for item in coverage for error in _validate_expected_coverage(item)]

    artifact_hashes: dict[str, str] = {}
    for item in queries:
        query_dir = output_dir / _safe_slug(str(item["id"]))
        for filename, _stage_name in STAGE_FILE_MAPPING:
            path = query_dir / filename
            if path.exists():
                artifact_hashes[path.relative_to(output_dir).as_posix()] = _hash_file(path)
        result_path = query_dir / "07_pipeline_result.json"
        if result_path.exists():
            artifact_hashes[result_path.relative_to(output_dir).as_posix()] = _hash_file(result_path)

    query_contract = [
        {
            "id": item["id"],
            "query": item["query"],
            "conversation_history": item.get("conversation_history") or [],
            "tags": item.get("tags") or [],
            "ministry": item.get("ministry"),
            "expected": item.get("expected") or {},
        }
        for item in queries
    ]
    database_prompt_hashes = _database_prompt_hashes()

    manifest = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "source_environment": args.source_environment,
        "reference_run_id": args.reference_run_id,
        "reference_run": reference_run,
        "query_count": len(queries),
        "succeeded_count": succeeded,
        "failed_count": len(errors),
        "errors": errors,
        "coverage_errors": coverage_errors,
        "coverage": coverage,
        "query_contract_fingerprint": _json_sha256(query_contract),
        "git_commit_sha": runtime_git_sha,
        "recorder_git_sha": recorder_git_sha,
        "pipeline_config": normalized_pipeline_config,
        "pipeline_config_fingerprint": _json_sha256(normalized_pipeline_config),
        "models": {
            "intent_model": pipe.config.query_processor.intent_model,
            "intent_provider": "albert",
            "generation_model": pipe.config.generation.model,
            "generation_provider": pipe.config.generation.provider.value,
            "generation_fallback_model": pipe.config.generation.fallback_model,
            "generation_fallback_provider": pipe.config.generation.fallback_provider.value,
            "selector_model": pipe.config.selector.model,
            "selector_provider": pipe.config.selector.provider.value,
            "embedding_model": pipe.config.retrieval.embedding_model.value,
            "embedding_primary_model": os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings"),
            "embedding_fallback": "bge_scaleway",
            "embedding_fallback_model": "bge-multilingual-gemma2",
        },
        "prompt_hashes": _prompt_hashes(),
        "database_prompt_hashes": database_prompt_hashes,
        "database_prompt_fingerprint": _json_sha256(database_prompt_hashes),
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
    }
    replay_fingerprint_payload = {key: value for key, value in manifest.items() if key not in {"generated_at", "replay_fingerprint"}}
    manifest["replay_fingerprint"] = _json_sha256(replay_fingerprint_payload)
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

    return 1 if errors or coverage_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
