#!/usr/bin/env python3
"""Capture a non-secret, reproducible parity snapshot for a recorded RAG eval.

The output deliberately records hashes and aggregate corpus state instead of
prompt bodies, gold answers, or database credentials. It is intended to be
versioned as evidence for the hexagonal API migration milestones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

try:
    from scripts.parity_revision import assert_runtime_revision_compatible, get_git_sha
except ModuleNotFoundError:  # Direct execution: python scripts/capture_rag_parity_evidence.py
    from parity_revision import assert_runtime_revision_compatible, get_git_sha

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROMPTS_DIR = REPO_ROOT / "packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts"

CORPUS_TABLES = (
    "rag_documents",
    "rag_sections",
    "rag_chunks_matte",
    "rag_chunks_service_public",
    "rag_chunks_dgafp",
    "rag_chunks_rgrh",
    "rag_chunks_mso",
    "rag_chunks_mi",
    "rag_chunks_masa",
)


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_columns(conn: psycopg.Connection[Any], table_name: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _corpus_table_snapshot(conn: psycopg.Connection[Any], table_name: str) -> dict[str, Any]:
    columns = _table_columns(conn, table_name)
    if not columns:
        return {"table": table_name, "exists": False}

    expressions: list[sql.Composable] = [sql.SQL("count(*) AS row_count")]
    for timestamp_column in ("created_at", "updated_at"):
        if timestamp_column in columns:
            identifier = sql.Identifier(timestamp_column)
            expressions.extend(
                [
                    sql.SQL("min({}) AS {}").format(identifier, sql.Identifier(f"min_{timestamp_column}")),
                    sql.SQL("max({}) AS {}").format(identifier, sql.Identifier(f"max_{timestamp_column}")),
                ]
            )

    identifier_column = next(
        (
            name
            for name in ("chunk_id", "section_id", "document_id", "hash_id", "cid", "short_id", "id")
            if name in columns
        ),
        None,
    )
    if identifier_column:
        identifier = sql.Identifier(identifier_column)
        expressions.extend(
            [
                sql.SQL("min({}::text) AS min_identifier").format(identifier),
                sql.SQL("max({}::text) AS max_identifier").format(identifier),
                sql.SQL("md5(string_agg({}::text, chr(10) ORDER BY {}::text)) AS identifier_fingerprint").format(
                    identifier,
                    identifier,
                ),
            ]
        )

    embedding_columns = sorted(name for name in columns if name.startswith("embedding"))
    for embedding_column in embedding_columns:
        identifier = sql.Identifier(embedding_column)
        alias = sql.Identifier(f"non_null_{embedding_column}")
        expressions.append(sql.SQL("count(*) FILTER (WHERE {} IS NOT NULL) AS {}").format(identifier, alias))

    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(expressions), sql.Identifier(table_name))
    row = dict(conn.execute(query).fetchone())
    return {
        "table": table_name,
        "exists": True,
        "identifier_column": identifier_column,
        "embedding_columns": embedding_columns,
        **row,
    }


def _corpus_snapshot(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    tables = [_corpus_table_snapshot(conn, table_name) for table_name in CORPUS_TABLES]
    return {"tables": tables, "fingerprint": _json_sha256(tables)}


def _db_prompt_snapshot(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT name, prompt_type, content, is_active, created_at, updated_at
        FROM system_prompts
        WHERE is_active IS TRUE
        ORDER BY name
        """
    ).fetchall()
    return [
        {
            "name": row["name"],
            "prompt_type": row["prompt_type"],
            "sha256": hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest(),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _local_prompt_snapshot() -> list[dict[str, str]]:
    if not LOCAL_PROMPTS_DIR.exists():
        return []
    return [
        {"name": path.name, "sha256": _file_sha256(path)}
        for path in sorted(LOCAL_PROMPTS_DIR.glob("*.md"))
        if path.is_file()
    ]


def _panel_snapshot(conn: psycopg.Connection[Any], question_ids: list[int]) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, question, gold_answer, gold_sources, gold_doc_ids, goldset_name,
               tags, source, updated_at
        FROM goldset_questions_v2
        WHERE id = ANY(%s)
        ORDER BY id
        """,
        (question_ids,),
    ).fetchall()
    private_fingerprint_rows = [dict(row) for row in rows]
    sources = Counter(str(row["source"] or "unknown") for row in rows)
    goldsets = Counter(str(row["goldset_name"] or "unknown") for row in rows)
    ids = [int(row["id"]) for row in rows]
    row_fingerprints = {str(row["id"]): _json_sha256(dict(row)) for row in rows if int(row["id"]) in (214, 676)}
    return {
        "question_count": len(rows),
        "question_ids": ids,
        "source_counts": dict(sorted(sources.items())),
        "goldset_counts": dict(sorted(goldsets.items())),
        "content_fingerprint": _json_sha256(private_fingerprint_rows),
        "known_debt_421": {
            "question_ids": [214, 676],
            "included_in_panel": all(question_id in ids for question_id in (214, 676)),
            "item_fingerprints": row_fingerprints,
        },
    }


def _per_corpus_metrics(conn: psycopg.Connection[Any], run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT coalesce(q.source, 'unknown') AS source,
               count(*) AS item_count,
               round(avg(CASE WHEN i.judge_result->>'pass' = 'true' THEN 1.0 ELSE 0.0 END)::numeric, 4) AS judge_pass_rate,
               round(avg((i.deterministic_metrics->>'hit_rate')::float)::numeric, 4) AS hit_rate,
               round(avg((i.deterministic_metrics->>'doc_recall')::float)::numeric, 4) AS doc_recall
        FROM rag_quality_eval_items i
        JOIN goldset_questions_v2 q ON q.id = i.question_id
        WHERE i.run_id = %s
        GROUP BY q.source
        ORDER BY q.source
        """,
        (run_id,),
    ).fetchall()
    return [
        {key: float(value) if isinstance(value, Decimal) else value for key, value in dict(row).items()}
        for row in rows
    ]


def _runtime_outcomes(conn: psycopg.Connection[Any], run_id: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT question_id, metadata, judge_result, error
        FROM rag_quality_eval_items
        WHERE run_id = %s
        ORDER BY question_id
        """,
        (run_id,),
    ).fetchall()
    selector_decisions: dict[str, list[int]] = {}
    generator_fallback_ids: list[int] = []
    selector_retry_ids: list[int] = []
    item_error_ids: list[int] = []
    generator_provider_counts: Counter[str] = Counter()
    generator_model_counts: Counter[str] = Counter()
    judge_status_counts: Counter[str] = Counter()

    for row in rows:
        question_id = int(row["question_id"])
        metadata = dict(row.get("metadata") or {})
        judge_result = dict(row.get("judge_result") or {})
        decision = str(metadata.get("selector_decision") or "not_run")
        selector_decisions.setdefault(decision, []).append(question_id)
        if metadata.get("generator_used_fallback"):
            generator_fallback_ids.append(question_id)
        if metadata.get("selector_retry_triggered"):
            selector_retry_ids.append(question_id)
        if str(row.get("error") or "").strip():
            item_error_ids.append(question_id)
        generator_provider_counts[str(metadata.get("generator_provider_used") or "unknown")] += 1
        generator_model_counts[str(metadata.get("generator_model_used") or "unknown")] += 1
        judge_status_counts[str(judge_result.get("status") or "unknown")] += 1

    return {
        "item_count": len(rows),
        "item_error_question_ids": item_error_ids,
        "generator_fallback_question_ids": generator_fallback_ids,
        "generator_provider_counts": dict(sorted(generator_provider_counts.items())),
        "generator_model_counts": dict(sorted(generator_model_counts.items())),
        "selector_decision_question_ids": dict(sorted(selector_decisions.items())),
        "selector_retry_question_ids": selector_retry_ids,
        "judge_status_counts": dict(sorted(judge_status_counts.items())),
    }


def _paired_baseline(conn: psycopg.Connection[Any], candidate: dict[str, Any], baseline_run_id: int) -> dict[str, Any]:
    baseline = conn.execute(
        """
        SELECT id, status, run_label, git_sha, goldset_name, tag_filter,
               config_fingerprint, aggregate, metadata
        FROM rag_quality_eval_runs
        WHERE id = %s
        """,
        (baseline_run_id,),
    ).fetchone()
    if baseline is None:
        raise RuntimeError(f"Paired baseline run {baseline_run_id} does not exist")
    baseline = dict(baseline)

    candidate_aggregate = dict(candidate.get("aggregate") or {})
    baseline_aggregate = dict(baseline.get("aggregate") or {})
    candidate_scope = dict((candidate.get("metadata") or {}).get("eval_scope") or {})
    baseline_scope = dict((baseline.get("metadata") or {}).get("eval_scope") or {})
    metric_names = ("judge_pass_rate", "doc_recall_avg", "hit_rate_avg", "retrieval_gap_rate")
    metrics = {}
    for metric_name in metric_names:
        candidate_value = candidate_aggregate.get(metric_name)
        baseline_value = baseline_aggregate.get(metric_name)
        metrics[metric_name] = {
            "candidate": candidate_value,
            "baseline": baseline_value,
            "delta": candidate_value - baseline_value if candidate_value is not None and baseline_value is not None else None,
        }

    candidate_by_source = {row["source"]: row for row in _per_corpus_metrics(conn, int(candidate["id"]))}
    baseline_by_source = {row["source"]: row for row in _per_corpus_metrics(conn, baseline_run_id)}
    per_corpus = []
    for source in sorted(set(candidate_by_source) | set(baseline_by_source)):
        candidate_row = candidate_by_source.get(source) or {}
        baseline_row = baseline_by_source.get(source) or {}
        row: dict[str, Any] = {"source": source, "candidate": candidate_row, "baseline": baseline_row, "deltas": {}}
        for metric_name in ("judge_pass_rate", "hit_rate", "doc_recall"):
            candidate_value = candidate_row.get(metric_name)
            baseline_value = baseline_row.get(metric_name)
            row["deltas"][metric_name] = (
                candidate_value - baseline_value if candidate_value is not None and baseline_value is not None else None
            )
        per_corpus.append(row)

    return {
        "run_id": baseline_run_id,
        "run_label": baseline["run_label"],
        "git_sha": baseline["git_sha"],
        "status": baseline["status"],
        "config_fingerprint": baseline["config_fingerprint"],
        "same_config_fingerprint": baseline["config_fingerprint"] == candidate.get("config_fingerprint"),
        "same_eval_scope": baseline_scope == candidate_scope,
        "same_goldset": baseline["goldset_name"] == candidate.get("goldset_name"),
        "same_tags": list(baseline.get("tag_filter") or []) == list(candidate.get("tag_filter") or []),
        "metrics": metrics,
        "per_corpus": per_corpus,
    }


def _models_and_providers(config: dict[str, Any]) -> dict[str, Any]:
    retrieval = dict(config.get("retrieval") or {})
    processor = dict(config.get("query_processor") or {})
    selector = dict(config.get("selector") or {})
    generation = dict(config.get("generation") or {})
    return {
        "intent": {"provider": "albert", "model": processor.get("intent_model")},
        "embedding": {
            "primary": {
                "key": retrieval.get("embedding_model"),
                "provider": "albert",
                "model": os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings"),
            },
            "fallback": {
                "key": "bge_scaleway",
                "provider": "scaleway",
                "model": "bge-multilingual-gemma2",
            },
        },
        "selector": {"provider": selector.get("provider"), "model": selector.get("model")},
        "generator": {
            "provider": generation.get("provider"),
            "model": generation.get("model"),
            "fallback_provider": generation.get("fallback_provider"),
            "fallback_model": generation.get("fallback_model"),
        },
    }


def capture_evidence(
    conn: psycopg.Connection[Any],
    run_id: int,
    expected_runtime_git_sha: str | None,
    paired_baseline_run_id: int | None,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    run = conn.execute(
        """
        SELECT id, status, goldset_name, tag_filter, git_sha, run_label,
               config_fingerprint, config, judge_provider, judge_model,
               ragas_status, created_at, completed_at, aggregate, metadata, error
        FROM rag_quality_eval_runs
        WHERE id = %s
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"RAG evaluation run {run_id} does not exist")

    run = dict(run)
    if run.get("status") != "completed" and not allow_incomplete:
        raise RuntimeError(f"RAG evaluation run {run_id} is {run.get('status')!r}, not completed")
    recorder_git_sha = get_git_sha(REPO_ROOT)
    runtime_git_sha = str(run.get("git_sha") or "")
    if not runtime_git_sha:
        raise RuntimeError(f"RAG evaluation run {run_id} has no Git revision")
    if expected_runtime_git_sha and runtime_git_sha != expected_runtime_git_sha:
        raise RuntimeError(f"Expected runtime Git SHA {expected_runtime_git_sha}, got {runtime_git_sha}")
    assert_runtime_revision_compatible(REPO_ROOT, runtime_git_sha, recorder_git_sha)

    metadata = dict(run.get("metadata") or {})
    eval_scope = dict(metadata.get("eval_scope") or {})
    question_ids = [int(value) for value in eval_scope.get("question_ids") or []]
    config = dict(run.get("config") or {})

    db_prompts = _db_prompt_snapshot(conn)
    local_prompts = _local_prompt_snapshot()
    evidence: dict[str, Any] = {
        "schema_version": "m0-parity-evidence-v1",
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "environment": "scaleway-staging",
        "repository": {"git_sha": runtime_git_sha, "recorder_git_sha": recorder_git_sha},
        "run": run,
        "models_and_providers": _models_and_providers(config),
        "prompts": {
            "database_active": db_prompts,
            "database_active_fingerprint": _json_sha256(db_prompts),
            "local_fallbacks": local_prompts,
            "local_fallbacks_fingerprint": _json_sha256(local_prompts),
        },
        "corpus": _corpus_snapshot(conn),
        "panel": _panel_snapshot(conn, question_ids),
        "runtime_outcomes": _runtime_outcomes(conn, run_id),
        "per_corpus_metrics": _per_corpus_metrics(conn, run_id),
    }
    if paired_baseline_run_id is not None:
        evidence["paired_baseline"] = _paired_baseline(conn, run, paired_baseline_run_id)
    fingerprint_payload = {key: value for key, value in evidence.items() if key not in {"captured_at", "evidence_fingerprint"}}
    evidence["evidence_fingerprint"] = _json_sha256(fingerprint_payload)
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture reproducible, non-secret evidence for a recorded RAG evaluation.")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument(
        "--expected-runtime-git-sha",
        "--expected-git-sha",
        dest="expected_runtime_git_sha",
        default="",
        help="Expected Git revision recorded by the eval run; --expected-git-sha is a compatibility alias.",
    )
    parser.add_argument("--paired-baseline-run-id", type=int, default=None)
    parser.add_argument("--allow-incomplete", action="store_true", help="Allow a diagnostic snapshot of a non-completed run.")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.getenv(args.dsn_env, "").strip().strip('"')
    if not dsn:
        raise RuntimeError(f"Missing DSN environment variable: {args.dsn_env}")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        evidence = capture_evidence(
            conn,
            args.run_id,
            args.expected_runtime_git_sha or None,
            args.paired_baseline_run_id,
            allow_incomplete=args.allow_incomplete,
        )

    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "evidence_fingerprint": evidence["evidence_fingerprint"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
