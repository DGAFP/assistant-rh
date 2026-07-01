"""Goldset-based RAG quality evaluation runner.

The runner is intentionally usable from CI and from a laptop:

* loads questions from ``goldset_questions_v2``;
* runs the production RAG pipeline with the same runtime config as Streamlit;
* records deterministic retrieval overlap against ``gold_sources``;
* optionally computes RAGAS metrics and an LLM-as-judge score;
* writes local artifacts and, when requested, durable DB run/item rows.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from assistant_rh_rag_pipeline import PipelineResult, create_pipeline
from assistant_rh_rag_pipeline.admin import get_rag_config, runtime_config_to_rag_config
from assistant_rh_rag_pipeline.config import RAGConfig
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".cache" / "assistant-rh" / "evals"
DEFAULT_JUDGE_MODEL = "qwen3-235b-a22b-instruct-2507"
DEFAULT_SCALEWAY_BASE_URL = "https://api.scaleway.ai/v1"
# RAGAS makes many statement/NLI calls per question; a large reasoning-grade
# model is overkill and slow there, so it defaults to a fast instruct model
# (the judge stays the higher-quality model). The token budget must be generous:
# on long French answers, faithfulness decomposition overflows a small cap and
# RAGAS then retries on every truncation, stalling the run.
DEFAULT_RAGAS_MODEL = "llama-3.3-70b-instruct"
DEFAULT_RAGAS_MAX_TOKENS = 16384


@dataclass
class GoldsetQuestion:
    id: int
    question: str
    gold_answer: str
    gold_sources: list[str]
    theme: str = ""
    tags: list[str] = field(default_factory=list)
    goldset_name: str = ""
    # Pre-resolved corpus doc_ids for ``gold_sources`` (deterministic retrieval
    # matching uses these when present; falls back to gold_sources otherwise).
    gold_doc_ids: list[str] = field(default_factory=list)

    @property
    def retrieval_gold(self) -> list[str]:
        return self.gold_doc_ids or self.gold_sources


@dataclass
class EvalItem:
    question_id: int
    question: str
    gold_answer: str
    gold_sources: list[str]
    answer: str = ""
    contexts: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    deterministic_metrics: dict[str, Any] = field(default_factory=dict)
    ragas_metrics: dict[str, Any] = field(default_factory=dict)
    judge_result: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class EvalSummary:
    run_id: int | None
    status: str
    goldset_name: str
    tag_filter: list[str]
    total: int
    completed: int
    failed: int
    aggregate: dict[str, Any]
    config_fingerprint: str
    output_json: str
    output_csv: str
    existing_run_id: int | None = None


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def resolve_dsn(explicit_dsn: str | None, dsn_env: str) -> str:
    if explicit_dsn:
        return explicit_dsn
    value = os.getenv(dsn_env, "").strip()
    if value:
        return value
    raise RuntimeError(f"No DSN found. Provide --dsn or set {dsn_env}.")


def parse_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in re.split(r"[;,]\s*", text) if part.strip()]


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def config_fingerprint(config: RAGConfig) -> str:
    payload = json.dumps(config.to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _column_exists(dsn: str, table_name: str, column_name: str) -> bool:
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s",
                (table_name, column_name),
            ).fetchone()
        return row is not None
    except psycopg.Error:
        return False


def load_goldset_questions(
    dsn: str,
    *,
    goldset_name: str,
    tags: list[str] | None = None,
    limit: int | None = None,
    any_goldset: bool = False,
) -> list[GoldsetQuestion]:
    # ``any_goldset`` selects a curated cross-goldset set purely by tag (the rows
    # keep their own goldset_name); ``goldset_name`` is then only the run label.
    if any_goldset and not tags:
        raise ValueError("At least one --tag is required with --any-goldset.")
    where = ["question IS NOT NULL", "btrim(question) <> ''"]
    params: list[Any] = []
    if not any_goldset:
        where.insert(0, "goldset_name = %s")
        params.append(goldset_name)
    if tags:
        where.append("tags && %s::text[]")
        params.append(tags)
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT %s"
        params.append(limit)

    # ``gold_doc_ids`` is an optional pre-resolution column; tolerate its absence.
    gold_doc_ids_col = "gold_doc_ids" if _column_exists(dsn, "goldset_questions_v2", "gold_doc_ids") else "NULL::text[] AS gold_doc_ids"
    sql = f"""
        SELECT id, question, gold_answer, gold_sources, theme, tags, goldset_name, {gold_doc_ids_col}
        FROM public.goldset_questions_v2
        WHERE {" AND ".join(where)}
        ORDER BY id
        {limit_sql}
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        GoldsetQuestion(
            id=int(row["id"]),
            question=str(row["question"] or ""),
            gold_answer=str(row.get("gold_answer") or ""),
            gold_sources=parse_text_list(row.get("gold_sources")),
            theme=str(row.get("theme") or ""),
            tags=parse_text_list(row.get("tags")),
            goldset_name=str(row.get("goldset_name") or ""),
            gold_doc_ids=parse_text_list(row.get("gold_doc_ids")),
        )
        for row in rows
    ]


def table_exists(dsn: str, table_name: str) -> bool:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = %s
            ) AS value
            """,
            (table_name,),
        ).fetchone()
    return bool(row["value"]) if row else False


def ensure_eval_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.rag_quality_eval_runs (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'started',
            goldset_name TEXT NOT NULL,
            tag_filter TEXT[] NOT NULL DEFAULT '{}',
            git_sha TEXT,
            run_label TEXT,
            config_fingerprint TEXT NOT NULL,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            judge_provider TEXT,
            judge_model TEXT,
            ragas_status TEXT,
            aggregate JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.rag_quality_eval_items (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES public.rag_quality_eval_runs(id) ON DELETE CASCADE,
            question_id BIGINT,
            question TEXT NOT NULL,
            gold_answer TEXT,
            gold_sources TEXT[] NOT NULL DEFAULT '{}',
            answer TEXT,
            contexts JSONB NOT NULL DEFAULT '[]'::jsonb,
            sources JSONB NOT NULL DEFAULT '[]'::jsonb,
            deterministic_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            ragas_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            judge_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            timing JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_runs_lookup "
        "ON public.rag_quality_eval_runs (goldset_name, config_fingerprint, status, created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_items_run_id ON public.rag_quality_eval_items (run_id)")


def find_existing_run(
    conn: psycopg.Connection[Any],
    *,
    goldset_name: str,
    config_hash: str,
    tags: list[str],
    git_sha: str,
    dedupe_scope: str,
    eval_scope: dict[str, Any],
) -> dict[str, Any] | None:
    git_sql = "AND git_sha = %s" if dedupe_scope == "config-and-git" else ""
    params: list[Any] = [goldset_name, config_hash, tags, json.dumps(eval_scope, sort_keys=True, ensure_ascii=False, default=str)]
    if dedupe_scope == "config-and-git":
        params.append(git_sha)
    rows = conn.execute(
        f"""
        SELECT id, status, created_at, completed_at, aggregate
        FROM public.rag_quality_eval_runs
        WHERE goldset_name = %s
          AND config_fingerprint = %s
          AND tag_filter = %s::text[]
          AND metadata -> 'eval_scope' = %s::jsonb
          {git_sql}
          AND status IN ('started', 'running', 'completed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        params,
    ).fetchall()
    return dict(rows[0]) if rows else None


def build_eval_scope(args: argparse.Namespace, questions: list[GoldsetQuestion]) -> dict[str, Any]:
    """Return the question and evaluator options that make a run reusable."""
    judge_enabled = not args.skip_judge
    ragas_enabled = not args.skip_ragas
    return {
        "limit": args.limit,
        "question_count": len(questions),
        "question_ids": [question.id for question in questions],
        "ragas_enabled": ragas_enabled,
        "ragas_model": args.ragas_model if ragas_enabled else "",
        "judge_enabled": judge_enabled,
        "judge_model": args.judge_model if judge_enabled else "",
    }


def create_eval_run(
    conn: psycopg.Connection[Any],
    *,
    goldset_name: str,
    tags: list[str],
    config: RAGConfig,
    config_hash: str,
    git_sha: str,
    run_label: str,
    judge_model: str,
    ragas_status: str,
    metadata: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO public.rag_quality_eval_runs (
            status, goldset_name, tag_filter, git_sha, run_label,
            config_fingerprint, config, judge_provider, judge_model,
            ragas_status, metadata
        )
        VALUES ('running', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            goldset_name,
            tags,
            git_sha,
            run_label,
            config_hash,
            json.dumps(config.to_dict(), ensure_ascii=False, default=str),
            "scaleway" if judge_model else "",
            judge_model,
            ragas_status,
            json.dumps(metadata, ensure_ascii=False, default=str),
        ),
    ).fetchone()
    return int(row["id"])


def insert_eval_item(conn: psycopg.Connection[Any], run_id: int, item: EvalItem) -> None:
    conn.execute(
        """
        INSERT INTO public.rag_quality_eval_items (
            run_id, question_id, question, gold_answer, gold_sources, answer,
            contexts, sources, deterministic_metrics, ragas_metrics, judge_result,
            timing, metadata, error
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            item.question_id,
            item.question,
            item.gold_answer,
            item.gold_sources,
            item.answer,
            json.dumps(item.contexts, ensure_ascii=False, default=str),
            json.dumps(item.sources, ensure_ascii=False, default=str),
            json.dumps(item.deterministic_metrics, ensure_ascii=False, default=str),
            json.dumps(item.ragas_metrics, ensure_ascii=False, default=str),
            json.dumps(item.judge_result, ensure_ascii=False, default=str),
            json.dumps(item.timing, ensure_ascii=False, default=str),
            json.dumps(item.metadata, ensure_ascii=False, default=str),
            item.error,
        ),
    )


def complete_eval_run(
    conn: psycopg.Connection[Any],
    *,
    run_id: int,
    status: str,
    aggregate: dict[str, Any],
    error: str = "",
) -> None:
    conn.execute(
        """
        UPDATE public.rag_quality_eval_runs
        SET status = %s,
            completed_at = now(),
            aggregate = %s,
            error = NULLIF(%s, '')
        WHERE id = %s
        """,
        (status, json.dumps(aggregate, ensure_ascii=False, default=str), error, run_id),
    )


def context_payload(result: PipelineResult) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    refs = result.metadata.get("context_items_ref")
    if not isinstance(refs, list):
        refs = []
    for index, item in enumerate(result.context_items):
        ref = refs[index] if index < len(refs) and isinstance(refs[index], dict) else {}
        payload.append(
            {
                "section_id": str(item.section_id or ""),
                "doc_id": str(ref.get("doc_id") or item.metadata.get("doc_id") or ""),
                "heading": item.heading,
                "publisher": item.publisher,
                "document_title": item.document_title,
                "document_url": item.document_url,
                "score": item.score,
                "token_estimate": item.token_estimate,
                "content": item.content,
                "metadata": item.metadata,
            }
        )
    return payload


_CODE_RE = re.compile(r"[FLRD]\.?\s?\d+(?:-\d+)?", re.IGNORECASE)
_RANGE_RE = re.compile(r"([LRD])\.?\s?(\d+)-(\d+)\s*à\s*[LRD]?\.?\s?\d+-(\d+)", re.IGNORECASE)


def _match_key(value: str) -> str:
    # Canonical key so heterogeneous identifiers compare correctly: uppercased,
    # whitespace and dots stripped, so an article code like "L. 332-22" matches
    # "L332-22". Harmless for UUIDs / F-fiche ids / LEGIARTI ids (no spaces/dots).
    return "".join(str(value or "").upper().split()).replace(".", "")


def load_gold_id_maps(dsn: str) -> dict[str, dict[str, Any]]:
    """Build the lookups that resolve human-facing ``gold_sources`` to the corpus
    ``doc_id``s the retriever actually returns: ``rag_documents.short_id`` (MATTE
    doc names) -> doc_id, ``rag_chunks_matte.short_id`` (annex codes) ->
    source_document_id, ``rag_chunks_dgafp.number`` (article codes) -> cid.
    Returns empty maps if the corpus tables are absent."""
    maps: dict[str, dict[str, Any]] = {"doc_short": {}, "matte_short": {}, "article": {}}
    queries = {
        "doc_short": "SELECT short_id, doc_id FROM public.rag_documents WHERE short_id IS NOT NULL",
        "matte_short": (
            "SELECT DISTINCT short_id, source_document_id AS v FROM public.rag_chunks_matte "
            "WHERE short_id IS NOT NULL AND source_document_id IS NOT NULL"
        ),
        "article": ("SELECT DISTINCT number AS short_id, cid AS v FROM public.rag_chunks_dgafp WHERE number IS NOT NULL AND cid IS NOT NULL"),
    }
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            for name, sql in queries.items():
                for row in conn.execute(sql).fetchall():
                    key = _match_key(row["short_id"])
                    value = str(row.get("doc_id") or row.get("v") or "").strip()
                    if not key or not value:
                        continue
                    if name == "doc_short":
                        maps[name][key] = value
                    else:
                        maps[name].setdefault(key, set()).add(value)
    except psycopg.Error:
        return {"doc_short": {}, "matte_short": {}, "article": {}}
    return maps


def resolve_gold_doc_ids(gold_sources: list[str], maps: dict[str, dict[str, Any]]) -> list[str]:
    """Resolve free-text ``gold_sources`` (F-fiche codes, MATTE doc names, annex
    codes, article codes and ranges) to corpus ``doc_id``s. Keep the raw token only
    when it could not be resolved, because resolved ids and raw labels are
    alternatives for the same expected source, not additional required sources."""
    resolved: list[str] = []
    for raw in gold_sources:
        raw = str(raw).strip()
        if not raw:
            continue
        resolved_for_raw: list[str] = []
        tokens = {raw, *_CODE_RE.findall(raw)}
        range_match = _RANGE_RE.search(raw)
        if range_match:
            prefix, base, start, end = range_match.group(1), range_match.group(2), int(range_match.group(3)), int(range_match.group(4))
            tokens.update(f"{prefix}{base}-{i}" for i in range(start, end + 1))
        for token in tokens:
            key = _match_key(token)
            if key in maps["article"]:
                resolved_for_raw.extend(maps["article"][key])
            if key in maps["matte_short"]:
                resolved_for_raw.extend(maps["matte_short"][key])
            if key in maps["doc_short"]:
                resolved_for_raw.append(maps["doc_short"][key])
        resolved.extend(resolved_for_raw or [raw])
    return _stable_unique(resolved)


def retrieved_doc_ids(result: PipelineResult, contexts: list[dict[str, Any]]) -> list[str]:
    ids = [str(ctx.get("doc_id") or "").strip() for ctx in contexts]
    for key in ("context_items_ref", "chunks_after_rerank", "chunks_raw", "retrieved_chunks"):
        values = result.metadata.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            ids.append(str(value.get("doc_id") or value.get("document_id") or "").strip())
    return _stable_unique(ids)


def deterministic_metrics(gold_sources: list[str], retrieved_ids: list[str]) -> dict[str, Any]:
    gold = _stable_unique(gold_sources)
    retrieved = _stable_unique(retrieved_ids)
    gold_set = {_match_key(g) for g in gold}
    retrieved_keys = [_match_key(r) for r in retrieved]
    retrieved_set = set(retrieved_keys)
    hits = [key for key in retrieved_keys if key in gold_set]
    reciprocal_rank = 0.0
    for idx, doc_id in enumerate(retrieved_keys, start=1):
        if doc_id in gold_set:
            reciprocal_rank = 1.0 / idx
            break
    return {
        "gold_count": len(gold),
        "retrieved_doc_count": len(retrieved),
        "hit_count": len(set(hits)),
        "doc_recall": len(gold_set & retrieved_set) / len(gold_set) if gold_set else None,
        "doc_precision": len(gold_set & retrieved_set) / len(retrieved_set) if retrieved_set else None,
        "hit_rate": 1.0 if gold_set & retrieved_set else 0.0,
        "mrr": reciprocal_rank,
        "missing_gold_sources": [source for source in gold if _match_key(source) not in retrieved_set],
        "retrieved_doc_ids": retrieved,
    }


def _metric_average(items: list[EvalItem], key: str, source: str = "deterministic_metrics") -> float | None:
    values: list[float] = []
    for item in items:
        metrics = getattr(item, source)
        value = metrics.get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    if not values:
        return None
    return sum(values) / len(values)


def _judge_dimension_average(items: list[EvalItem], key: str) -> float | None:
    values: list[float] = []
    for item in items:
        dimensions = item.judge_result.get("dimensions")
        if not isinstance(dimensions, dict):
            continue
        value = dimensions.get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    if not values:
        return None
    return sum(values) / len(values)


def aggregate_items(items: list[EvalItem]) -> dict[str, Any]:
    judged = [item for item in items if item.judge_result.get("status") == "completed"]
    return {
        "total": len(items),
        "completed": sum(1 for item in items if not item.error),
        "failed": sum(1 for item in items if item.error),
        "doc_recall_avg": _metric_average(items, "doc_recall"),
        "doc_precision_avg": _metric_average(items, "doc_precision"),
        "hit_rate_avg": _metric_average(items, "hit_rate"),
        "mrr_avg": _metric_average(items, "mrr"),
        "judge_score_avg": _metric_average(items, "score", "judge_result"),
        "judge_raw_score_avg": _metric_average(items, "raw_model_score", "judge_result"),
        "judge_pass_rate": sum(1 for item in judged if item.judge_result.get("pass") is True) / len(judged) if judged else None,
        "judge_legal_correctness_avg": _judge_dimension_average(items, "legal_correctness"),
        "judge_completeness_avg": _judge_dimension_average(items, "completeness"),
        "judge_gold_answer_alignment_avg": _judge_dimension_average(items, "gold_answer_alignment"),
        "judge_source_support_avg": _judge_dimension_average(items, "source_support"),
        "ragas_faithfulness_avg": _metric_average(items, "faithfulness", "ragas_metrics"),
        "ragas_context_precision_avg": _metric_average(items, "context_precision", "ragas_metrics"),
        "ragas_context_recall_avg": _metric_average(items, "context_recall", "ragas_metrics"),
    }


def compute_ragas_metrics(
    *,
    question: str,
    answer: str,
    contexts: list[str],
    reference: str,
    model: str,
    base_url: str,
    api_key: str,
) -> dict[str, Any]:
    if not api_key:
        return {"status": "skipped", "reason": "missing SCALEWAY_API_KEY"}
    try:
        from datasets import Dataset
        from openai import OpenAI
        from ragas import evaluate
        from ragas.llms import llm_factory
        from ragas.metrics import context_precision, context_recall, faithfulness
    except ImportError as exc:
        return {"status": "skipped", "reason": f"missing dependency: {exc.name}"}

    try:
        dataset = Dataset.from_list(
            [
                {
                    "user_input": question,
                    "response": answer,
                    "retrieved_contexts": contexts,
                    "reference": reference,
                }
            ]
        )
        client = OpenAI(api_key=api_key, base_url=base_url)
        llm = llm_factory(
            model=model,
            provider="openai",
            client=client,
            max_tokens=int(os.getenv("RAGAS_MAX_TOKENS", str(DEFAULT_RAGAS_MAX_TOKENS))),
        )
        metrics = [copy.deepcopy(faithfulness), copy.deepcopy(context_precision), copy.deepcopy(context_recall)]
        result = evaluate(dataset, metrics=metrics, llm=llm, show_progress=False)
        frame = result.to_pandas()
        row = frame.iloc[0].to_dict()
        return {
            "status": "completed",
            "faithfulness": _safe_float(row.get("faithfulness")),
            "context_precision": _safe_float(row.get("context_precision")),
            "context_recall": _safe_float(row.get("context_recall")),
        }
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "oui", "1"}
    return bool(value)


def _dimension_score(dimensions: dict[str, Any], key: str) -> float:
    value = _safe_float(dimensions.get(key))
    if value is None:
        return 0.0
    return min(1.0, max(0.0, value))


# --- Judge calibration rubric -------------------------------------------------
# Every magic number used by ``calibrate_judge_result`` lives here so the scoring
# weights, the score-capping floors, and the (intentionally stricter, independent)
# pass-gate floors are visible in one place and cannot silently drift apart.

JUDGE_DIMENSION_WEIGHTS: dict[str, float] = {
    "legal_correctness": 0.35,
    "completeness": 0.25,
    "gold_answer_alignment": 0.25,
    "source_support": 0.15,
}

# When a dimension is below ``floor`` the final score is capped at ``max_score``.
# Order is preserved in the emitted ``calibration_caps`` list.
JUDGE_DIMENSION_SCORE_CAPS: list[tuple[str, str, float, float]] = [
    ("gold_answer_alignment", "weak_gold_answer_alignment", 0.75, 0.75),
    ("legal_correctness", "legal_correctness_below_gate", 0.8, 0.75),
    ("completeness", "incomplete_answer", 0.7, 0.8),
    ("source_support", "weak_source_support", 0.7, 0.75),
]

MATERIAL_CONTRADICTION_CAP = 0.6
NO_EXPECTED_SOURCE_CAP = 0.6
MISSING_EXPECTED_SOURCE_CAP = 0.85

# Pass gate: minimum final score and per-dimension floors required to pass.
# These are intentionally stricter than the score-cap floors above.
JUDGE_PASS_MIN_SCORE = 0.8
JUDGE_PASS_DIMENSION_FLOORS: dict[str, float] = {
    "legal_correctness": 0.8,
    "completeness": 0.75,
    "gold_answer_alignment": 0.8,
}


@dataclass(frozen=True)
class JudgeRubric:
    """All tunable knobs of the judge calibration in one injectable bundle, so a
    calibration run can vary thresholds without mutating module state. The default
    (``DEFAULT_JUDGE_RUBRIC``) reproduces the module constants exactly."""

    dimension_weights: dict[str, float]
    dimension_score_caps: tuple[tuple[str, str, float, float], ...]
    material_contradiction_cap: float
    no_expected_source_cap: float
    missing_expected_source_cap: float
    pass_min_score: float
    pass_dimension_floors: dict[str, float]


DEFAULT_JUDGE_RUBRIC = JudgeRubric(
    dimension_weights=JUDGE_DIMENSION_WEIGHTS,
    dimension_score_caps=tuple(JUDGE_DIMENSION_SCORE_CAPS),
    material_contradiction_cap=MATERIAL_CONTRADICTION_CAP,
    no_expected_source_cap=NO_EXPECTED_SOURCE_CAP,
    missing_expected_source_cap=MISSING_EXPECTED_SOURCE_CAP,
    pass_min_score=JUDGE_PASS_MIN_SCORE,
    pass_dimension_floors=JUDGE_PASS_DIMENSION_FLOORS,
)


def calibrate_judge_result(parsed: dict[str, Any], deterministic: dict[str, Any], rubric: JudgeRubric = DEFAULT_JUDGE_RUBRIC) -> dict[str, Any]:
    dimensions_raw = parsed.get("dimensions")
    dimensions = dimensions_raw if isinstance(dimensions_raw, dict) else {}
    normalized_dimensions = {dim: _dimension_score(dimensions, dim) for dim in rubric.dimension_weights}
    weighted_score = sum(normalized_dimensions[dim] * weight for dim, weight in rubric.dimension_weights.items())
    raw_model_score = _safe_float(parsed.get("score"))
    candidate_score = min(weighted_score, raw_model_score) if raw_model_score is not None else weighted_score

    caps: list[dict[str, Any]] = []
    material_contradiction = _bool_value(parsed.get("material_contradiction"))
    if material_contradiction:
        caps.append({"reason": "material_contradiction_with_gold_answer", "max_score": rubric.material_contradiction_cap})
    for dim, reason, floor, max_score in rubric.dimension_score_caps:
        if normalized_dimensions[dim] < floor:
            caps.append({"reason": reason, "max_score": max_score})

    # A total miss of declared expected sources is still a hard cap. Partial
    # recall is a soft cap: it lowers the stored score for visibility, but does
    # not independently block a pass when answer quality remains above threshold.
    # ``doc_recall=None`` (empty gold set) yields no retrieval cap at all.
    doc_recall = deterministic.get("doc_recall")
    hit_rate = deterministic.get("hit_rate")
    soft_caps: list[dict[str, Any]] = []
    if isinstance(doc_recall, int | float):
        if hit_rate == 0.0:
            caps.append({"reason": "no_expected_source_retrieved", "max_score": rubric.no_expected_source_cap})
        elif doc_recall < 1.0:
            soft_caps.append({"reason": "missing_expected_source", "max_score": rubric.missing_expected_source_cap, "soft": True})

    final_score = candidate_score
    score_caps = caps + soft_caps
    if score_caps:
        final_score = min(final_score, *(float(cap["max_score"]) for cap in score_caps))
    final_score = min(1.0, max(0.0, final_score))

    failure_category = str(parsed.get("failure_category") or "none")
    pass_value = (
        final_score >= rubric.pass_min_score
        and not material_contradiction
        and not caps
        and all(normalized_dimensions[dim] >= floor for dim, floor in rubric.pass_dimension_floors.items())
    )

    parsed["raw_model_score"] = raw_model_score
    parsed["score"] = final_score
    parsed["pass"] = pass_value
    parsed["failure_category"] = "none" if pass_value else failure_category if failure_category != "none" else "quality_gate_failed"
    parsed["dimensions"] = normalized_dimensions
    parsed["calibration_caps"] = caps + soft_caps
    parsed["material_contradiction"] = material_contradiction
    return parsed


def judge_answer(
    *,
    question: str,
    gold_answer: str,
    answer: str,
    contexts: list[str],
    deterministic_metrics: dict[str, Any],
    model: str,
    base_url: str,
    api_key: str,
) -> dict[str, Any]:
    if not api_key:
        return {"status": "skipped", "reason": "missing SCALEWAY_API_KEY"}
    try:
        from openai import OpenAI
    except ImportError as exc:
        return {"status": "skipped", "reason": f"missing dependency: {exc.name}"}

    prompt = {
        "question": question,
        "gold_answer": gold_answer,
        "candidate_answer": answer,
        "retrieved_contexts": contexts[:8],
        "retrieval_diagnostics": {
            "doc_recall": deterministic_metrics.get("doc_recall"),
            "doc_precision": deterministic_metrics.get("doc_precision"),
            "hit_rate": deterministic_metrics.get("hit_rate"),
            "missing_gold_sources": deterministic_metrics.get("missing_gold_sources"),
        },
        "rubric": {
            "dimensions": {
                "legal_correctness": (
                    "0.0 to 1.0. Penalize a wrong legal rule, wrong obligation, wrong exception, or misleading nuance. "
                    "A correct answer scores high even if briefly stated."
                ),
                "completeness": (
                    "0.0 to 1.0. Judge ONLY whether the elements REQUIRED to act on the question are present. "
                    "Score 1.0 when every legally required condition/deadline/exception in the gold answer is covered, "
                    "even if the candidate is shorter and omits optional extras, examples, or extra context the gold answer did not require. "
                    "Do NOT demand exhaustiveness and do NOT penalize concision. "
                    "Only lower the score when a REQUIRED element from the gold answer is missing."
                ),
                "gold_answer_alignment": (
                    "0.0 to 1.0. Penalize a contradiction or a weaker/stronger legal conclusion than the gold answer. "
                    "Extra correct information, or covering a broader public than asked, is NOT a misalignment."
                ),
                "source_support": (
                    "0.0 to 1.0. Penalize claims that are unsupported by the gold answer or contexts. "
                    "If the answer's substantive claims match the gold answer, score high."
                ),
            },
            "score": "your uncalibrated overall score from 0.0 to 1.0 before code-side caps",
            "pass": "true only when the answer is legally correct, covers the required points, aligns with the gold answer, and is source-supported",
            "failure_category": "one of: none, wrong_law, incomplete, unsupported, hallucination, refusal, irrelevant",
            "material_contradiction": (
                "true ONLY when the candidate directly asserts the OPPOSITE of a legally material point in the gold answer "
                "(e.g. eligible vs not eligible, owed vs not owed, allowed vs forbidden). "
                "Differences of emphasis, extra correct detail, addressing a broader audience, or merely omitting a point are NOT contradictions."
            ),
        },
    }
    system = (
        "You are a French public-sector HR RAG evaluator. Judge correctness and faithfulness to the gold answer, NOT style, "
        "tone, audience (whether it addresses the agent or the HR manager), length, or formatting — those are never quality failures here. "
        "Compare the candidate answer against the gold answer first, then against retrieved contexts. "
        "Reward a correct, source-grounded answer even when it is concise; do not require exhaustiveness. "
        "Do not reward a fluent answer that contradicts the gold answer on a material legal point. "
        "Return only valid JSON with keys: score, pass, failure_category, material_contradiction, "
        "dimensions, missing_required_points, contradictions, rationale, source_support."
    )
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content or "{}"
        parsed = _extract_json_object(content)
        parsed["status"] = "completed"
        return calibrate_judge_result(parsed, deterministic_metrics)
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def run_question(
    *,
    pipe: Any,
    question: GoldsetQuestion,
    run_ragas: bool,
    run_judge: bool,
    judge_model: str,
    ragas_model: str,
    scaleway_base_url: str,
    scaleway_api_key: str,
) -> EvalItem:
    started = time.perf_counter()
    item = EvalItem(
        question_id=question.id,
        question=question.question,
        gold_answer=question.gold_answer,
        gold_sources=question.gold_sources,
    )
    try:
        result = pipe.run_with_trace(question.question)
        elapsed_ms = (time.perf_counter() - started) * 1000
        contexts = context_payload(result)
        doc_ids = retrieved_doc_ids(result, contexts)

        item.answer = result.answer
        item.contexts = contexts
        item.sources = result.sources
        item.timing = {**result.timing, "eval_total_ms": elapsed_ms}
        item.metadata = result.metadata
        item.deterministic_metrics = deterministic_metrics(question.retrieval_gold, doc_ids)

        context_texts = [str(context.get("content") or "") for context in contexts if str(context.get("content") or "").strip()]
        if run_ragas:
            item.ragas_metrics = compute_ragas_metrics(
                question=question.question,
                answer=result.answer,
                contexts=context_texts,
                reference=question.gold_answer,
                model=ragas_model,
                base_url=scaleway_base_url,
                api_key=scaleway_api_key,
            )
        else:
            item.ragas_metrics = {"status": "skipped", "reason": "disabled"}

        if run_judge:
            item.judge_result = judge_answer(
                question=question.question,
                gold_answer=question.gold_answer,
                answer=result.answer,
                contexts=context_texts,
                deterministic_metrics=item.deterministic_metrics,
                model=judge_model,
                base_url=scaleway_base_url,
                api_key=scaleway_api_key,
            )
        else:
            item.judge_result = {"status": "skipped", "reason": "disabled"}
    except Exception as exc:
        item.error = str(exc)
    return item


def artifact_paths(output_dir: Path, run_label: str) -> tuple[Path, Path]:
    """Deterministic JSON/CSV artifact paths for a run, shared by the writer and
    the DB metadata so a recorded run can be traced back to its local files."""
    return output_dir / f"{run_label}.json", output_dir / f"{run_label}.csv"


def write_artifacts(output_dir: Path, run_label: str, items: list[EvalItem], summary: dict[str, Any]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = artifact_paths(output_dir, run_label)
    json_path.write_text(
        json.dumps({"summary": summary, "items": [asdict(item) for item in items]}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    fieldnames = [
        "question_id",
        "question",
        "theme",
        "gold_sources",
        "doc_recall",
        "doc_precision",
        "hit_rate",
        "mrr",
        "judge_score",
        "judge_raw_score",
        "judge_pass",
        "judge_failure_category",
        "judge_legal_correctness",
        "judge_completeness",
        "judge_gold_answer_alignment",
        "judge_source_support",
        "judge_material_contradiction",
        "judge_calibration_caps",
        "ragas_faithfulness",
        "ragas_context_precision",
        "ragas_context_recall",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            dimensions = item.judge_result.get("dimensions")
            if not isinstance(dimensions, dict):
                dimensions = {}
            writer.writerow(
                {
                    "question_id": item.question_id,
                    "question": item.question,
                    "theme": item.metadata.get("theme", ""),
                    "gold_sources": json.dumps(item.gold_sources, ensure_ascii=False),
                    "doc_recall": item.deterministic_metrics.get("doc_recall"),
                    "doc_precision": item.deterministic_metrics.get("doc_precision"),
                    "hit_rate": item.deterministic_metrics.get("hit_rate"),
                    "mrr": item.deterministic_metrics.get("mrr"),
                    "judge_score": item.judge_result.get("score"),
                    "judge_raw_score": item.judge_result.get("raw_model_score"),
                    "judge_pass": item.judge_result.get("pass"),
                    "judge_failure_category": item.judge_result.get("failure_category"),
                    "judge_legal_correctness": dimensions.get("legal_correctness"),
                    "judge_completeness": dimensions.get("completeness"),
                    "judge_gold_answer_alignment": dimensions.get("gold_answer_alignment"),
                    "judge_source_support": dimensions.get("source_support"),
                    "judge_material_contradiction": item.judge_result.get("material_contradiction"),
                    "judge_calibration_caps": json.dumps(item.judge_result.get("calibration_caps") or [], ensure_ascii=False),
                    "ragas_faithfulness": item.ragas_metrics.get("faithfulness"),
                    "ragas_context_precision": item.ragas_metrics.get("context_precision"),
                    "ragas_context_recall": item.ragas_metrics.get("context_recall"),
                    "error": item.error,
                }
            )
    return json_path, csv_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RAG quality evaluation on a goldset.")
    parser.add_argument("--goldset-name", required=True, help="goldset_questions_v2.goldset_name to evaluate.")
    parser.add_argument("--tag", action="append", default=[], help="Require at least one tag. Repeatable.")
    parser.add_argument(
        "--any-goldset",
        action="store_true",
        help="Select across all goldsets by --tag (goldset-name becomes only the run label).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of questions to evaluate.")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN, overrides --dsn-env.")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN", help="Environment variable containing the target DSN.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for JSON/CSV artifacts.")
    parser.add_argument("--run-label", default="", help="Human-readable run label. Defaults to timestamp + goldset.")
    parser.add_argument("--record-db", action="store_true", help="Persist run and item rows to DB.")
    parser.add_argument("--init-schema", action="store_true", help="Create eval tables/indexes before recording.")
    parser.add_argument("--skip-if-started", action="store_true", help="Skip when a matching started/running/completed run already exists.")
    parser.add_argument(
        "--dedupe-scope",
        choices=["config-and-git", "config"],
        default="config-and-git",
        help="Run de-duplication key. CI should keep config-and-git so base and head are both evaluated.",
    )
    parser.add_argument("--skip-ragas", action="store_true", help="Skip RAGAS metrics.")
    parser.add_argument("--skip-judge", action="store_true", help="Skip Scaleway LLM-as-judge.")
    parser.add_argument("--judge-model", default=os.getenv("SCALEWAY_JUDGE_MODEL", DEFAULT_JUDGE_MODEL), help="Scaleway judge model.")
    parser.add_argument(
        "--ragas-model",
        default=os.getenv("RAGAS_MODEL", DEFAULT_RAGAS_MODEL),
        help="Scaleway model for RAGAS metrics (fast instruct model; separate from the judge).",
    )
    parser.add_argument("--scaleway-base-url", default=os.getenv("SCALEWAY_BASE_URL", DEFAULT_SCALEWAY_BASE_URL), help="OpenAI-compatible base URL.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first item failure.")
    return parser


def run_eval(args: argparse.Namespace) -> EvalSummary:
    if args.any_goldset and not args.tag:
        raise ValueError("At least one --tag is required with --any-goldset.")

    dsn = resolve_dsn(args.dsn, args.dsn_env)
    # The pipeline and prompt/config helpers read the canonical runtime DSN.
    # Bind the explicitly selected eval target for this process so staging
    # evals do not accidentally fetch prompts/config from a local Supabase DSN.
    os.environ["SCW_POSTGRES_DSN"] = dsn
    runtime_config = get_rag_config()
    pipeline_config = runtime_config_to_rag_config(runtime_config)
    config_adjustments: list[str] = []
    if pipeline_config.retrieval.enable_chunks_test and not table_exists(dsn, "rag_chunks_test"):
        pipeline_config.retrieval.enable_chunks_test = False
        config_adjustments.append("disabled missing optional table rag_chunks_test")
    config_hash = config_fingerprint(pipeline_config)
    git_sha = _git_sha()
    run_label = args.run_label or f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}_{args.goldset_name}"
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / args.goldset_name / run_label)

    json_path, csv_path = artifact_paths(output_dir, run_label)

    questions = load_goldset_questions(dsn, goldset_name=args.goldset_name, tags=args.tag, limit=args.limit, any_goldset=args.any_goldset)
    if not questions:
        raise RuntimeError(f"No questions found for goldset={args.goldset_name!r}, tags={args.tag!r}.")
    eval_scope = build_eval_scope(args, questions)

    run_id: int | None = None
    existing_run: dict[str, Any] | None = None
    if args.record_db or args.skip_if_started:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            if args.init_schema:
                ensure_eval_schema(conn)
                conn.commit()
            existing_run = find_existing_run(
                conn,
                goldset_name=args.goldset_name,
                config_hash=config_hash,
                tags=args.tag,
                git_sha=git_sha,
                dedupe_scope=args.dedupe_scope,
                eval_scope=eval_scope,
            )
            if existing_run and args.skip_if_started:
                summary = {
                    "status": "skipped_existing",
                    "existing_run_id": existing_run["id"],
                    "existing_status": existing_run["status"],
                    "config_fingerprint": config_hash,
                    "eval_scope": eval_scope,
                }
                json_path, csv_path = write_artifacts(output_dir, run_label, [], summary)
                return EvalSummary(
                    run_id=None,
                    status="skipped_existing",
                    goldset_name=args.goldset_name,
                    tag_filter=args.tag,
                    total=len(questions),
                    completed=0,
                    failed=0,
                    aggregate=summary,
                    config_fingerprint=config_hash,
                    output_json=str(json_path),
                    output_csv=str(csv_path),
                    existing_run_id=int(existing_run["id"]),
                )
            if args.record_db:
                run_id = create_eval_run(
                    conn,
                    goldset_name=args.goldset_name,
                    tags=args.tag,
                    config=pipeline_config,
                    config_hash=config_hash,
                    git_sha=git_sha,
                    run_label=run_label,
                    judge_model=args.judge_model if not args.skip_judge else "",
                    ragas_status="skipped" if args.skip_ragas else "requested",
                    metadata={
                        "created_by": "scripts/run_rag_quality_eval.py",
                        "started_at": _now_iso(),
                        "config_adjustments": config_adjustments,
                        "dedupe_scope": args.dedupe_scope,
                        "eval_scope": eval_scope,
                        "output_json": str(json_path),
                        "output_csv": str(csv_path),
                    },
                )
                conn.commit()

    pipe = create_pipeline(config=pipeline_config, dsn=dsn)
    api_key = os.getenv("SCALEWAY_API_KEY", "").strip()
    items: list[EvalItem] = []
    status = "completed"
    error = ""

    try:
        for question in questions:
            item = run_question(
                pipe=pipe,
                question=question,
                run_ragas=not args.skip_ragas,
                run_judge=not args.skip_judge,
                judge_model=args.judge_model,
                ragas_model=args.ragas_model,
                scaleway_base_url=args.scaleway_base_url,
                scaleway_api_key=api_key,
            )
            items.append(item)
            if run_id is not None:
                with psycopg.connect(dsn, row_factory=dict_row) as conn:
                    insert_eval_item(conn, run_id, item)
                    conn.commit()
            if item.error and args.fail_fast:
                raise RuntimeError(item.error)
        if any(item.error for item in items):
            status = "completed_with_errors"
        # A judge/RAGAS sub-task that was requested but errored on every evaluated
        # item is a configuration failure (bad key, renamed model), not a passing
        # run. "skipped" results (e.g. missing API key) are intentional and ignored.
        for label, enabled, attr in (
            ("judge", not args.skip_judge, "judge_result"),
            ("ragas", not args.skip_ragas, "ragas_metrics"),
        ):
            if not enabled:
                continue
            sub_statuses = [getattr(item, attr).get("status") for item in items if not item.error]
            if sub_statuses and all(sub_status == "failed" for sub_status in sub_statuses):
                status = "failed"
                error = error or f"{label} requested but failed on all {len(sub_statuses)} evaluated items"
    except Exception as exc:
        status = "failed"
        error = str(exc)
        if run_id is None:
            raise
    finally:
        aggregate = aggregate_items(items)
        aggregate.update(
            {
                "goldset_name": args.goldset_name,
                "tag_filter": args.tag,
                "config_fingerprint": config_hash,
                "ragas_enabled": not args.skip_ragas,
                "judge_enabled": not args.skip_judge,
                "judge_model": args.judge_model if not args.skip_judge else "",
                "ragas_model": args.ragas_model if not args.skip_ragas else "",
                "config_adjustments": config_adjustments,
                "git_sha": git_sha,
                "dedupe_scope": args.dedupe_scope,
                "eval_scope": eval_scope,
                "judge_failed": sum(1 for item in items if item.judge_result.get("status") == "failed"),
                "ragas_failed": sum(1 for item in items if item.ragas_metrics.get("status") == "failed"),
            }
        )
        if run_id is not None:
            with psycopg.connect(dsn, row_factory=dict_row) as conn:
                complete_eval_run(conn, run_id=run_id, status=status, aggregate=aggregate, error=error)
                conn.commit()

    json_path, csv_path = write_artifacts(
        output_dir,
        run_label,
        items,
        {
            "run_id": run_id,
            "status": status,
            "goldset_name": args.goldset_name,
            "tag_filter": args.tag,
            "config_fingerprint": config_hash,
            "eval_scope": eval_scope,
            "aggregate": aggregate,
            "error": error,
        },
    )
    return EvalSummary(
        run_id=run_id,
        status=status,
        goldset_name=args.goldset_name,
        tag_filter=args.tag,
        total=len(questions),
        completed=sum(1 for item in items if not item.error),
        failed=sum(1 for item in items if item.error),
        aggregate=aggregate,
        config_fingerprint=config_hash,
        output_json=str(json_path),
        output_csv=str(csv_path),
        # A fresh run was recorded as ``run_id``; ``existing_run_id`` is only
        # meaningful on the skipped-existing path, which returns earlier.
        existing_run_id=None,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args(argv)
    summary = run_eval(args)
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2, default=str))
    return 0 if summary.status not in {"failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
