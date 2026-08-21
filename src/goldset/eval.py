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
from assistant_rh_rag_pipeline.models import metadata_document_id
from assistant_rh_rag_pipeline.query_processor import _fold as _fold_text
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".cache" / "assistant-rh" / "evals"
# Juge LLM: OpenRouter/Grok 4.5 (décision 2026-07-21, revue #329). L'exigence
# ZDR stricte (`zdr: true`, distincte de data_collection=deny) est non
# négociable pour des données réglementaires DINUM — or qwen3.7-max (juge
# depuis le 16/07, spot-check favorable) n'a AUCUN endpoint ZDR. grok-4.5
# était le backup validé du même spot-check (« comportement proche ») et
# dispose d'un endpoint ZDR (xAI). claude-sonnet-4.5 (ZDR Bedrock) reste
# écarté : over-strict ; glm-5.2 (ZDR AtlasCloud) : verdicts incohérents.
# Surchargeable au run (--judge-model / OPENROUTER_JUDGE_MODEL).
DEFAULT_JUDGE_PROVIDER = "openrouter"
DEFAULT_JUDGE_MODEL = "x-ai/grok-4.5"
DEFAULT_JUDGE_BASE_URL = "https://openrouter.ai/api/v1"
# Bascule du 19/08/2026 (banc des juges, journal) : mistral-medium-3.5 —
# accord 88,8 % avec l'ancien protocole à rubrique amendée, calage de sévérité
# identique, 1 % de votes partagés, ~20 s/verdict maj-3 (10× plus rapide).
# L'ancien souverain qwen3-235b reste accessible via SCALEWAY_JUDGE_MODEL.
DEFAULT_SCALEWAY_JUDGE_MODEL = "mistral-medium-3.5-128b"
ALLOWED_JUDGE_VOTES = frozenset({1, 3})
# RAGAS reste sur Scaleway/OpenAI-compat (le SDK ragas attend un endpoint
# embeddings+LLM compatible; Claude via OpenRouter n'y est pas branché).
DEFAULT_SCALEWAY_BASE_URL = "https://api.scaleway.ai/v1"
# RAGAS makes many statement/NLI calls per question; a large reasoning-grade
# model is overkill and slow there, so it defaults to a fast instruct model
# (the judge stays the higher-quality model). The token budget must be generous:
# on long French answers, faithfulness decomposition overflows a small cap and
# RAGAS then retries on every truncation, stalling the run.
DEFAULT_RAGAS_MODEL = "llama-3.3-70b-instruct"
DEFAULT_RAGAS_MAX_TOKENS = 16384

# Endpoint du juge par provider: le provider pilote RÉELLEMENT la clé ET la base
# URL (revue #318: sinon --judge-provider scaleway inscrivait "scaleway" en base
# mais utilisait la clé/endpoint OpenRouter). ``default_base_url`` sert de secours
# quand ni --judge-base-url ni la var d'env n'est fournie.
JUDGE_PROVIDERS: dict[str, dict[str, str]] = {
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "url_env": "OPENROUTER_BASE_URL",
        "default_base_url": DEFAULT_JUDGE_BASE_URL,
        "model_env": "OPENROUTER_JUDGE_MODEL",
        "default_model": DEFAULT_JUDGE_MODEL,
    },
    "scaleway": {
        "key_env": "SCALEWAY_API_KEY",
        "url_env": "SCALEWAY_BASE_URL",
        "default_base_url": DEFAULT_SCALEWAY_BASE_URL,
        "model_env": "SCALEWAY_JUDGE_MODEL",
        "default_model": DEFAULT_SCALEWAY_JUDGE_MODEL,
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.openai.com/v1",
        "model_env": "OPENAI_JUDGE_MODEL",
        "default_model": "",
    },
}


def resolve_judge_endpoint(provider: str | None, explicit_base_url: str | None = None) -> tuple[str, str, str]:
    """Résout (provider, base_url, api_key) du juge à partir du provider.

    Le base_url explicite (--judge-base-url) prime, sinon la var d'env du
    provider, sinon son default. La clé vient TOUJOURS de la var d'env du
    provider — jamais d'un provider inscrit sans clé correspondante."""
    resolved = (provider or DEFAULT_JUDGE_PROVIDER).strip().lower()
    defaults = JUDGE_PROVIDERS.get(resolved) or JUDGE_PROVIDERS[DEFAULT_JUDGE_PROVIDER]
    base_url = (explicit_base_url or "").strip() or os.getenv(defaults["url_env"], "").strip() or defaults["default_base_url"]
    api_key = os.getenv(defaults["key_env"], "").strip()
    return resolved, base_url, api_key


def resolve_judge_model(provider: str | None, explicit_model: str | None = None) -> str:
    """Resolve the judge model from the selected provider, never from another provider's defaults."""
    resolved = (provider or DEFAULT_JUDGE_PROVIDER).strip().lower()
    defaults = JUDGE_PROVIDERS.get(resolved)
    if defaults is None:
        raise ValueError(f"Unsupported judge provider: {resolved!r}")
    model = (explicit_model or "").strip() or os.getenv(defaults["model_env"], "").strip() or defaults["default_model"]
    if not model:
        raise ValueError(f"No judge model configured for provider {resolved!r}; pass --judge-model or set {defaults['model_env']}.")
    return model


def normalize_judge_votes(value: Any) -> int:
    """Return a supported vote count shared by scoping and execution."""
    try:
        votes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("judge_votes must be 1 (screening) or 3 (official adoption gate)") from exc
    if votes not in ALLOWED_JUDGE_VOTES:
        allowed = ", ".join(str(item) for item in sorted(ALLOWED_JUDGE_VOTES))
        raise ValueError(f"judge_votes must be one of: {allowed}")
    return votes


@dataclass
class GoldsetQuestion:
    id: int
    question: str
    gold_answer: str
    gold_sources: list[str]
    theme: str = ""
    tags: list[str] = field(default_factory=list)
    goldset_name: str = ""
    # Corpus d'origine de la question (MATTE/MSO/MI/Service-Public/manual…):
    # pilote le scope ministériel par question (--ministry-scope per-question).
    source: str = ""
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

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # ``gold_doc_ids`` est une colonne de PRÉ-RÉSOLUTION (labels humains ->
        # doc_ids corpus): sans elle, le matching retombe sur les labels bruts
        # (F-fiches, noms de docs) qui ne matchent jamais les UUID/short_ids du
        # retriever, et le juge reçoit de faux ``retrieval_diagnostics``
        # (hit_rate=0, missing_gold_sources). La présence de la colonne est
        # vérifiée sur LA MÊME connexion que la requête: un probe sur une
        # connexion séparée (ancien ``_column_exists``, ``except psycopg.Error:
        # return False``) transformait une erreur transitoire en dégradation
        # SILENCIEUSE de tout le run (run 67, 06/07/2026: gold_doc_ids ignorés,
        # hit_rate/doc_recall corrompus, juge biaisé sur MATTE/MSO). Ici toute
        # erreur se propage — fail loud plutôt que corrompre en silence.
        has_gold_doc_ids = (
            conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='goldset_questions_v2' AND column_name='gold_doc_ids'"
            ).fetchone()
            is not None
        )
        gold_doc_ids_col = "gold_doc_ids" if has_gold_doc_ids else "NULL::text[] AS gold_doc_ids"
        sql = f"""
            SELECT id, question, gold_answer, gold_sources, theme, tags, goldset_name, source, {gold_doc_ids_col}
            FROM public.goldset_questions_v2
            WHERE {" AND ".join(where)}
            ORDER BY id
            {limit_sql}
        """
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
            source=str(row.get("source") or ""),
            gold_doc_ids=parse_text_list(row.get("gold_doc_ids")),
        )
        for row in rows
    ]


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
    # Mirror the Supabase migration so the --init-schema bootstrap and the migration
    # converge. No current query filters on question_id alone, so this is for parity.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_items_question_id ON public.rag_quality_eval_items (question_id)")


def _eval_scope_variants(eval_scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current scope plus semantically equivalent legacy encodings."""
    optional_legacy_keys: list[str] = []
    if eval_scope.get("ministry_scope") == "none":
        optional_legacy_keys.append("ministry_scope")
    legacy_votes = 1 if eval_scope.get("judge_enabled") else 0
    if eval_scope.get("judge_votes") == legacy_votes:
        optional_legacy_keys.append("judge_votes")

    variants = [dict(eval_scope)]
    for key in optional_legacy_keys:
        variants.extend({item_key: item_value for item_key, item_value in variant.items() if item_key != key} for variant in list(variants))
    return variants


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
    scope_variants = _eval_scope_variants(eval_scope)
    params: list[Any] = [
        goldset_name,
        config_hash,
        tags,
        [json.dumps(variant, sort_keys=True, ensure_ascii=False, default=str) for variant in scope_variants],
    ]
    if dedupe_scope == "config-and-git":
        params.append(git_sha)
    rows = conn.execute(
        f"""
        SELECT id, status, created_at, completed_at, aggregate
        FROM public.rag_quality_eval_runs
        WHERE goldset_name = %s
          AND config_fingerprint = %s
          AND tag_filter = %s::text[]
          AND metadata -> 'eval_scope' = ANY(%s::jsonb[])
          {git_sql}
          AND status IN ('started', 'running', 'completed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        params,
    ).fetchall()
    return dict(rows[0]) if rows else None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _same_text_list(left: Any, right: Any) -> bool:
    return parse_text_list(left) == parse_text_list(right)


def _load_eval_run(
    conn: psycopg.Connection[Any],
    *,
    run_id: int | None = None,
    run_label: str = "",
    goldset_name: str = "",
    tags: list[str] | None = None,
    eval_scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if run_id is not None:
        rows = conn.execute(
            """
            SELECT id, status, created_at, completed_at, goldset_name, tag_filter,
                   git_sha, run_label, config_fingerprint, aggregate, metadata, error
            FROM public.rag_quality_eval_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchall()
    elif run_label:
        rows = conn.execute(
            """
            SELECT id, status, created_at, completed_at, goldset_name, tag_filter,
                   git_sha, run_label, config_fingerprint, aggregate, metadata, error
            FROM public.rag_quality_eval_runs
            WHERE run_label = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_label,),
        ).fetchall()
    else:
        if not goldset_name or tags is None or eval_scope is None:
            return None
        # Accept legacy encodings for fields whose missing value had the same
        # runtime semantics (ministry_scope=none and judge_votes=1/0).
        scope_variants = _eval_scope_variants(eval_scope)
        rows = conn.execute(
            """
            SELECT id, status, created_at, completed_at, goldset_name, tag_filter,
                   git_sha, run_label, config_fingerprint, aggregate, metadata, error
            FROM public.rag_quality_eval_runs
            WHERE goldset_name = %s
              AND tag_filter = %s::text[]
              AND metadata -> 'eval_scope' = ANY(%s::jsonb[])
              AND status = 'completed'
            ORDER BY completed_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (goldset_name, tags, [json.dumps(variant, sort_keys=True, ensure_ascii=False, default=str) for variant in scope_variants]),
        ).fetchall()
    return dict(rows[0]) if rows else None


def _baseline_is_comparable(
    baseline_run: dict[str, Any],
    *,
    goldset_name: str,
    tags: list[str],
    eval_scope: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    metadata = _json_dict(baseline_run.get("metadata"))
    baseline_scope = _json_dict(metadata.get("eval_scope"))
    # Les runs antérieurs au 06/07/2026 n'avaient pas de scope ministériel —
    # comportement identique à ministry_scope="none". Sans ce backfill, l'ajout
    # de la clé rendait TOUTES les baselines historiques non comparables, y
    # compris pour un candidat "none" pourtant à périmètre strictement
    # identique (le gate CI COMPARE_BASELINE échouait jusqu'à re-baseline).
    if "ministry_scope" in eval_scope and "ministry_scope" not in baseline_scope:
        baseline_scope = {**baseline_scope, "ministry_scope": "none"}
    legacy_votes = 1 if eval_scope.get("judge_enabled") else 0
    if eval_scope.get("judge_votes") == legacy_votes and "judge_votes" not in baseline_scope:
        baseline_scope = {**baseline_scope, "judge_votes": legacy_votes}
    if baseline_run.get("status") != "completed":
        reasons.append(f"baseline status is {baseline_run.get('status')!r}, expected 'completed'")
    if str(baseline_run.get("goldset_name") or "") != goldset_name:
        reasons.append("baseline goldset_name does not match candidate")
    if not _same_text_list(baseline_run.get("tag_filter"), tags):
        reasons.append("baseline tag_filter does not match candidate")
    if baseline_scope != eval_scope:
        reasons.append("baseline eval_scope does not match candidate")
    return not reasons, reasons


def compare_with_baseline(
    *,
    candidate_aggregate: dict[str, Any],
    baseline_run: dict[str, Any] | None,
    goldset_name: str,
    tags: list[str],
    eval_scope: dict[str, Any],
    max_judge_pass_rate_drop: float,
    max_doc_recall_drop: float,
) -> dict[str, Any]:
    if baseline_run is None:
        return {"status": "missing_baseline", "passed": False, "failures": ["No comparable baseline run found."]}

    comparable, reasons = _baseline_is_comparable(baseline_run, goldset_name=goldset_name, tags=tags, eval_scope=eval_scope)
    baseline_aggregate = _json_dict(baseline_run.get("aggregate"))
    comparison: dict[str, Any] = {
        "status": "passed",
        "passed": True,
        "baseline_run_id": baseline_run.get("id"),
        "baseline_run_label": baseline_run.get("run_label"),
        "baseline_git_sha": baseline_run.get("git_sha"),
        "baseline_config_fingerprint": baseline_run.get("config_fingerprint"),
        "comparable": comparable,
        "comparability_failures": reasons,
        "metrics": {},
        "failures": [],
    }
    if not comparable:
        comparison["status"] = "not_comparable"
        comparison["passed"] = False
        comparison["failures"].extend(reasons)
        return comparison

    thresholds = {
        "judge_pass_rate": max_judge_pass_rate_drop,
        "doc_recall_avg": max_doc_recall_drop,
    }
    for metric, max_drop in thresholds.items():
        baseline_value = _safe_float(baseline_aggregate.get(metric))
        candidate_value = _safe_float(candidate_aggregate.get(metric))
        delta = candidate_value - baseline_value if baseline_value is not None and candidate_value is not None else None
        metric_result = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "max_drop": max_drop,
            "passed": delta is not None and delta >= -max_drop,
        }
        if delta is None:
            metric_result["reason"] = "missing_metric"
        comparison["metrics"][metric] = metric_result
        if not metric_result["passed"]:
            if delta is None:
                comparison["failures"].append(f"{metric} missing")
            else:
                comparison["failures"].append(f"{metric} dropped by {abs(delta):.4f}; max allowed is {max_drop:.4f}")

    if comparison["failures"]:
        comparison["status"] = "failed"
        comparison["passed"] = False
    return comparison


def baseline_comparison_requested(args: argparse.Namespace) -> bool:
    return bool(args.baseline_run_id or args.baseline_run_label or args.require_baseline or args.gate_against_baseline)


def build_baseline_comparison(
    conn: psycopg.Connection[Any],
    *,
    args: argparse.Namespace,
    candidate_aggregate: dict[str, Any],
    goldset_name: str,
    tags: list[str],
    eval_scope: dict[str, Any],
) -> dict[str, Any]:
    baseline_run = _load_eval_run(
        conn,
        run_id=args.baseline_run_id,
        run_label=args.baseline_run_label,
        goldset_name=goldset_name,
        tags=tags,
        eval_scope=eval_scope,
    )
    return compare_with_baseline(
        candidate_aggregate=candidate_aggregate,
        baseline_run=baseline_run,
        goldset_name=goldset_name,
        tags=tags,
        eval_scope=eval_scope,
        max_judge_pass_rate_drop=args.max_judge_pass_rate_drop,
        max_doc_recall_drop=args.max_doc_recall_drop,
    )


def baseline_gate_failed(args: argparse.Namespace, comparison: dict[str, Any]) -> bool:
    if args.gate_against_baseline:
        return not bool(comparison.get("passed"))
    if args.require_baseline:
        return comparison.get("status") in {"missing_baseline", "not_comparable"}
    return False


def build_eval_scope(args: argparse.Namespace, questions: list[GoldsetQuestion]) -> dict[str, Any]:
    """Return the question and evaluator options that make a run reusable."""
    judge_enabled = not args.skip_judge
    ragas_enabled = not args.skip_ragas
    judge_votes = normalize_judge_votes(getattr(args, "judge_votes", 1)) if judge_enabled else 0
    return {
        "limit": args.limit,
        "question_count": len(questions),
        "question_ids": [question.id for question in questions],
        "ragas_enabled": ragas_enabled,
        "ragas_model": args.ragas_model if ragas_enabled else "",
        "judge_enabled": judge_enabled,
        "judge_provider": getattr(args, "judge_provider", DEFAULT_JUDGE_PROVIDER) if judge_enabled else "",
        # base URL résolue (pas seulement le provider/modèle): deux runs sur des
        # endpoints différents ne sont PAS comparables (revue #318). Sans elle,
        # un smoke run pouvait réutiliser un résultat produit ailleurs.
        "judge_base_url": resolve_judge_endpoint(getattr(args, "judge_provider", DEFAULT_JUDGE_PROVIDER), getattr(args, "judge_base_url", None))[1]
        if judge_enabled
        else "",
        "judge_model": args.judge_model if judge_enabled else "",
        # Vote majoritaire du juge : un run jugé en maj-3 (protocole officiel
        # d'adoption, juge souverain) n'est PAS comparable à un run single-shot
        # (screening intermédiaire grok) — la clé de scope les sépare.
        "judge_votes": judge_votes,
        # Partie de la clé de comparabilité: un run scopé « all ministries »
        # n'est pas comparable à un run historique sans scope.
        "ministry_scope": getattr(args, "ministry_scope", "none"),
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
    judge_provider: str,
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
            judge_provider if judge_model else "",
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
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    refs = metadata.get("context_items_ref")
    if not isinstance(refs, list):
        refs = []
    for index, item in enumerate(result.context_items):
        item_metadata = item.metadata if isinstance(item.metadata, dict) else {}
        ref = refs[index] if index < len(refs) and isinstance(refs[index], dict) else {}
        payload.append(
            {
                "section_id": str(item.section_id if item.section_id is not None else ""),
                "doc_id": str(ref.get("doc_id") or item_metadata.get("doc_id") or ""),
                "heading": item.heading,
                "publisher": item.publisher,
                "document_title": item.document_title,
                "document_url": item.document_url,
                "score": item.score,
                "token_estimate": item.token_estimate,
                "content": item.content,
                "metadata": item_metadata,
            }
        )
    return payload


# _CODE_RE keeps every hyphenated segment ("-\d+" repeated) so a multi-segment code
# like "L. 332-22-1" is captured whole instead of collapsing to the parent "L332-22".
# TODO(pr212-review): _RANGE_RE still reuses only the first base ("L. 331-1 à L. 335-9"
# expands to L331-* only), dropping cross-base ranges — understates the diagnostic-only
# retrieval metrics (doc_recall/hit_rate ne gatent plus le judge pass depuis le
# 06/07/2026 — caps retrieval passés en soft, cf. calibrate_judge_result).
# Same-base ranges (the common form) are fine.
_CODE_RE = re.compile(r"[FLRD]\.?\s?\d+(?:-\d+)*", re.IGNORECASE)
_RANGE_RE = re.compile(r"([LRD])\.?\s?(\d+)-(\d+)\s*à\s*[LRD]?\.?\s?\d+-(\d+)", re.IGNORECASE)
_DECREE_NUMBER_RE = re.compile(r"\b(?:décret|decret)\s*(?:n[°o]\s*)?(\d{2,4}-\d+)\b", re.IGNORECASE)
_ARTICLE_RANGE_RE = re.compile(r"\barticles?\s+(\d+(?:-\d+)?)\s*(?:à|a)\s*(\d+(?:-\d+)?)\b", re.IGNORECASE)
_ARTICLE_SINGLE_RE = re.compile(r"\b(?:article|art\.)\s*(\d+(?:-\d+)?)\b", re.IGNORECASE)
_ARTICLE_LIST_RE = re.compile(r"\barticles?\s+((?:\d+(?:-\d+)?)(?:\s*(?:,|;|et)\s*\d+(?:-\d+)?)+)", re.IGNORECASE)


def _match_key(value: str) -> str:
    # Canonical key so heterogeneous identifiers compare correctly: uppercased,
    # whitespace and dots stripped, so an article code like "L. 332-22" matches
    # "L332-22". Harmless for UUIDs / F-fiche ids / LEGIARTI ids (no spaces/dots).
    return "".join(str(value or "").upper().split()).replace(".", "")


def _ascii_lower(value: str) -> str:
    # Réutilise le fold canonique du pipeline: NFKD seul ne décompose PAS les
    # tirets typographiques (U+2011 insécable, U+2212 moins — courants dans les
    # collages PDF/Légifrance), et « Décret 86‑83 » échouait alors
    # _DECREE_NUMBER_RE, abandonnant la résolution legal_ref en silence.
    return _fold_text(str(value or ""))


def _alias_key(value: str) -> str:
    return _match_key(value)


def _add_alias_group(aliases: dict[str, set[str]], values: list[str]) -> None:
    clean_values = _stable_unique([str(value or "").strip() for value in values if str(value or "").strip()])
    for value in clean_values:
        key = _alias_key(value)
        if not key:
            continue
        aliases.setdefault(key, set()).update(v for v in clean_values if _alias_key(v) != key)


def _legal_ref_key(decree_number: str, article_number: str) -> str:
    return f"DECREE:{_match_key(decree_number)}:ARTICLE:{_match_key(article_number)}"


def _extract_decree_number(value: str) -> str | None:
    match = _DECREE_NUMBER_RE.search(_ascii_lower(value))
    return match.group(1) if match else None


def _numeric_article_range(start: str, end: str) -> list[str]:
    """Expand same-base article ranges such as 10 à 15.

    Hyphenated article ranges with different suffixes are intentionally not
    guessed; they are uncommon in the goldset and ambiguous without corpus data.
    """
    if "-" in start or "-" in end:
        return [start, end] if start != end else [start]
    start_i = int(start)
    end_i = int(end)
    if start_i > end_i or end_i - start_i > 100:
        return [start, end]
    return [str(value) for value in range(start_i, end_i + 1)]


def _extract_article_numbers(value: str) -> list[str]:
    text = _ascii_lower(value)
    numbers: list[str] = []
    for match in _ARTICLE_RANGE_RE.finditer(text):
        numbers.extend(_numeric_article_range(match.group(1), match.group(2)))
    for match in _ARTICLE_LIST_RE.finditer(text):
        numbers.extend(re.findall(r"\d+(?:-\d+)?", match.group(1)))
    for match in _ARTICLE_SINGLE_RE.finditer(text):
        numbers.append(match.group(1))
    return _stable_unique(numbers)


def load_gold_id_maps(dsn: str) -> dict[str, dict[str, Any]]:
    """Build the lookups that resolve human-facing ``gold_sources`` to the corpus
    ``doc_id``s the retriever actually returns: ``rag_documents.short_id`` (MATTE
    doc names) -> doc_id, ``rag_chunks_matte.short_id`` (annex codes) ->
    source_document_id, ``rag_chunks_dgafp.number`` (article codes) -> cid.
    Intentionally do not resolve against ``rag_chunks_legifrance``: that table
    contains legacy full-text Légifrance residue that the production retriever
    does not query, so crediting it would inflate eval results.

    The returned ``aliases`` map captures corpus-equivalent identifiers. This is
    needed because the DGAFP retriever often logs a Légifrance ``cid``/short_id
    (``LEGIARTI...``), while pre-resolved gold rows may contain the corresponding
    ``rag_documents.doc_id`` UUID.

    Toute erreur DB se propage — même doctrine fail-loud que le probe
    ``gold_doc_ids`` de ``load_goldset_questions``: un ``except psycopg.Error``
    qui rendait des maps vides transformait une erreur transitoire (ou une
    colonne absente) en dégradation SILENCIEUSE de tout le run — résolution
    runtime et alias désactivés, métriques hit_rate/doc_recall corrompues,
    juge biaisé (mécanisme du run 67, 06/07/2026)."""
    maps: dict[str, dict[str, Any]] = {"doc_short": {}, "matte_short": {}, "article": {}, "legal_ref": {}, "aliases": {}}
    queries = {
        "doc_short": "SELECT short_id, doc_id, source_url FROM public.rag_documents WHERE short_id IS NOT NULL",
        "matte_short": (
            "SELECT DISTINCT short_id, source_document_id AS v FROM public.rag_chunks_matte "
            "WHERE short_id IS NOT NULL AND source_document_id IS NOT NULL"
        ),
        "article": (
            "SELECT DISTINCT number AS short_id, cid AS v, title, full_title "
            "FROM public.rag_chunks_dgafp WHERE number IS NOT NULL AND cid IS NOT NULL"
        ),
    }
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for name, sql in queries.items():
            for row in conn.execute(sql).fetchall():
                key = _match_key(row["short_id"])
                value = str(row.get("doc_id") or row.get("v") or "").strip()
                if not key or not value:
                    continue
                if name == "doc_short":
                    maps[name][key] = value
                    aliases = maps["aliases"]
                    alias_values = [value, str(row["short_id"])]
                    source_url = str(row.get("source_url") or "").strip()
                    if source_url:
                        alias_values.append(source_url)
                        alias_values.append(source_url.rstrip("/").rsplit("/", 1)[-1])
                    _add_alias_group(aliases, alias_values)
                else:
                    maps[name].setdefault(key, set()).add(value)
                    if name == "article":
                        title = str(row.get("title") or "")
                        full_title = str(row.get("full_title") or "")
                        decree_number = _extract_decree_number(f"{title} {full_title}")
                        if decree_number:
                            maps["legal_ref"].setdefault(_legal_ref_key(decree_number, str(row["short_id"])), set()).add(value)
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

        decree_number = _extract_decree_number(raw)
        if decree_number:
            for article_number in _extract_article_numbers(raw):
                resolved_for_raw.extend(maps.get("legal_ref", {}).get(_legal_ref_key(decree_number, article_number), []))

        for token in tokens:
            key = _match_key(token)
            if key in maps.get("article", {}):
                resolved_for_raw.extend(maps["article"][key])
            if key in maps.get("matte_short", {}):
                resolved_for_raw.extend(maps["matte_short"][key])
            if key in maps.get("doc_short", {}):
                resolved_for_raw.append(maps["doc_short"][key])
        resolved.extend(resolved_for_raw or [raw])
    return _stable_unique(resolved)


def merge_gold_doc_ids(
    pre_resolved: list[str],
    gold_sources: list[str],
    maps: dict[str, dict[str, Any]],
) -> list[str]:
    """Union de la colonne ``gold_doc_ids`` pré-résolue et de la résolution
    runtime des ``gold_sources``.

    Régression #276: ``run_eval`` remplaçait ``gold_doc_ids`` par
    ``resolve_gold_doc_ids(gold_sources, maps)`` — mais cette résolution ne sait
    pas mapper les codes MATTE/MSO (F-fiche, annexe) vers un doc_id (les
    short_ids ministériels sont des hex, pas des F-codes). La colonne
    pré-résolue porte ces résolutions (pont par similarité de titres) que
    l'écrasement jetait: ``hit_rate=0`` sur MATTE/MSO alors que le gold est
    retrouvé, et juge biaisé par de faux ``missing_gold_sources`` (runs 67/68).

    On garde le meilleur des deux mondes: résolution runtime (#276: DGAFP,
    décrets, alias LEGIARTI) ∪ colonne curée (pont MATTE/MSO). Le matching
    ``hit_rate`` est un « au moins un recouvrement », donc l'union ne peut que
    rétablir des hits légitimes sans en inventer.

    Un label brut (valeur qui est elle-même un ``gold_source``, non résolu vers
    un doc_id) et un id corpus sont des ALTERNATIVES pour la même source
    attendue, pas des sources supplémentaires. On écarte les labels bruts
    RÉSIDUELS DE LA COLONNE curée: l'ancien ``resolve_goldset_doc_ids`` écrivait
    ``resolved_for_raw or [raw]`` en base, laissant des F-codes/sentences décret
    À CÔTÉ du pont UUID (46/116 lignes staging au 06/07/2026). Les garder rendait
    ``doc_recall < 1.0`` structurel (un F-code ne matche jamais un UUID),
    déclenchait le cap soft ``missing_expected_source`` à chaque question et
    nourrissait le juge de faux ``missing_gold_sources``. Le filtre ne s'applique
    qu'en présence d'AU MOINS un doc_id corpus dans l'union; comme il porte sur
    ``pre_resolved``, ré-exécuter ``scripts/resolve_goldset_doc_ids.py`` nettoie
    la colonne en place.

    En revanche on GARDE le passthrough runtime d'une source réellement
    irrésoluble qui n'est PAS déjà dans la colonne: c'est son seul ancrage
    (alias/url-tail), et une AUTRE source résolue sur la même ligne ne doit pas
    le faire disparaître (sinon doc_recall gonflé, retrieval gap masqué). Si rien
    ne résout, on conserve tous les labels bruts."""
    runtime_resolved = resolve_gold_doc_ids(gold_sources, maps)
    merged = _stable_unique([*(pre_resolved or []), *runtime_resolved])
    source_keys = {_match_key(source) for source in gold_sources if str(source).strip()}
    resolved_ids = [value for value in merged if _match_key(value) not in source_keys]
    # Aucun doc_id corpus: garder les labels bruts (seul ancrage possible).
    if not resolved_ids:
        return merged
    # Un label brut n'est écarté que s'il traînait DANS la colonne curée à côté
    # d'un id résolu (leftover de l'ancien script). Le passthrough runtime d'une
    # source non couverte (absente de la colonne) reste son unique ancrage.
    pre_keys = {_match_key(value) for value in (pre_resolved or [])}
    return [value for value in merged if _match_key(value) not in source_keys or _match_key(value) not in pre_keys]


def retrieved_doc_ids(result: PipelineResult, contexts: list[dict[str, Any]]) -> list[str]:
    def ids_from_ref(value: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        for key in ("doc_id", "document_id", "source_document_id", "cid", "short_id", "doc_short_id", "document_short_id"):
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                ids.append(str(raw).strip())
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            ids.extend(ids_from_ref(metadata))
        return ids

    ids: list[str] = []
    for ctx in contexts:
        ids.extend(ids_from_ref(ctx))
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    for key in ("context_items_ref", "chunks_after_rerank", "chunks_raw", "retrieved_chunks"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            ids.extend(ids_from_ref(value))
    return _stable_unique(ids)


def _expand_identifier_aliases(values: list[str], aliases: dict[str, set[str]] | None) -> list[str]:
    if not aliases:
        return _stable_unique(values)
    expanded: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean:
            continue
        expanded.append(clean)
        expanded.extend(aliases.get(_alias_key(clean), []))
    return _stable_unique(expanded)


def deterministic_metrics(gold_sources: list[str], retrieved_ids: list[str], aliases: dict[str, set[str]] | None = None) -> dict[str, Any]:
    gold = _expand_identifier_aliases(gold_sources, aliases)
    retrieved = _expand_identifier_aliases(retrieved_ids, aliases)
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


# Funnel d'observabilité retrieval (issue selector v4, 08/2026) : recall mesuré
# à chaque étage, par tentative. Les k sections tracés encadrent la fenêtre du
# selector (12 candidats évalués, 20 sections agrégées), puis le dernier étage
# reflète le contexte effectivement produit par ContextBuilder.
_STAGE_SECTION_KS = (12, 20)


def _attempt_stage_doc_ids(attempt: dict[str, Any]) -> dict[str, list[str]]:
    """Doc ids présents à chaque étage d'une tentative de retrieval.

    Étages : ``pool`` (chunks avant rerank), ``sections_top{k}`` (sections
    agrégées post-rerank, ordre présenté au selector), ``selector_kept``
    (sections retenues par le selector) et ``context_builder_output`` (items
    effectivement servis). Un étage absent de la trace est omis, pas inventé.
    """
    stages: dict[str, list[str]] = {}

    chunks = attempt.get("chunks_before_rerank")
    if isinstance(chunks, list):
        stages["pool"] = [str(c.get("doc_id")) for c in chunks if isinstance(c, dict) and c.get("doc_id")]

    aggregated = attempt.get("aggregated_sections")
    if isinstance(aggregated, list):
        for k in _STAGE_SECTION_KS:
            stages[f"sections_top{k}"] = [str(s.get("document_id")) for s in aggregated[:k] if isinstance(s, dict) and s.get("document_id")]
    else:
        aggregated = []

    kept = ((attempt.get("selector") or {}).get("decisions") or {}).get("kept")
    if isinstance(kept, list):
        kept_ids: list[str] = []
        for entry in kept:
            if not isinstance(entry, dict):
                continue
            doc_id = entry.get("document_id")
            if not doc_id:
                idx = entry.get("idx")
                if isinstance(idx, int) and 0 <= idx < len(aggregated):
                    doc_id = aggregated[idx].get("document_id")
            if doc_id:
                kept_ids.append(str(doc_id))
        stages["selector_kept"] = kept_ids

    context_items = attempt.get("context_items_ref")
    if isinstance(context_items, list):
        context_doc_ids: list[str] = []
        for item in context_items:
            if not isinstance(item, dict):
                continue
            nested_metadata = item.get("metadata")
            doc_id = metadata_document_id(item, nested_metadata if isinstance(nested_metadata, dict) else {})
            if doc_id:
                context_doc_ids.append(doc_id)
        stages["context_builder_output"] = context_doc_ids
    return stages


def stage_retrieval_metrics(
    metadata: dict[str, Any],
    gold_sources: list[str],
    aliases: dict[str, set[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Hit/recall par étage du funnel retrieval, pour chaque tentative tracée.

    Répond à « le gold a-t-il été perdu au retrieval, à l'agrégation, à la coupe
    top-k ou au selector ? » sans rejouer le pipeline. Le contexte final servi
    reste mesuré par ``deterministic_metrics`` (inchangé) : ces étages sont un
    diagnostic amont, pas un gate.
    """
    attempts = metadata.get("retrieval_attempts")
    if not isinstance(attempts, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for position, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            continue
        name = str(attempt.get("name") or f"attempt_{position}")
        attempt_stages: dict[str, Any] = {}
        for stage, ids in _attempt_stage_doc_ids(attempt).items():
            raw_doc_count = len(_stable_unique([str(doc_id).strip() for doc_id in ids if str(doc_id).strip()]))
            metrics = deterministic_metrics(gold_sources, ids, aliases=aliases)
            attempt_stages[stage] = {
                "hit_rate": metrics["hit_rate"],
                "doc_recall": metrics["doc_recall"],
                "doc_count": raw_doc_count,
            }
        if attempt_stages:
            out[name] = attempt_stages
    return out


def _aggregate_stage_metrics(items: list[EvalItem]) -> dict[str, Any]:
    """Moyennes par tentative et par étage sur les items du run."""
    collected: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in items:
        stages = (item.deterministic_metrics or {}).get("stages")
        if not isinstance(stages, dict):
            continue
        for attempt_name, attempt_stages in stages.items():
            if not isinstance(attempt_stages, dict):
                continue
            for stage, metrics in attempt_stages.items():
                if isinstance(metrics, dict):
                    collected.setdefault(attempt_name, {}).setdefault(stage, []).append(metrics)
    aggregated: dict[str, Any] = {}
    for attempt_name, attempt_stages in collected.items():
        aggregated[attempt_name] = {}
        for stage, metric_list in attempt_stages.items():
            hit_rates = [m["hit_rate"] for m in metric_list if m.get("hit_rate") is not None]
            recalls = [m["doc_recall"] for m in metric_list if m.get("doc_recall") is not None]
            aggregated[attempt_name][stage] = {
                "n": len(metric_list),
                "hit_rate_avg": sum(hit_rates) / len(hit_rates) if hit_rates else None,
                "doc_recall_avg": sum(recalls) / len(recalls) if recalls else None,
            }
    return aggregated


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
        # Part des questions dont AUCUN gold doc n'est retrouvé (hit_rate=0):
        # diagnostic retrieval découplé du judge_pass depuis le 06/07/2026
        # (le cap n'est plus bloquant) — à surveiller côté pipeline/goldset.
        "retrieval_gap_rate": (
            sum(1 for item in items if item.deterministic_metrics.get("hit_rate") == 0.0)
            / len([item for item in items if item.deterministic_metrics.get("hit_rate") is not None])
            if any(item.deterministic_metrics.get("hit_rate") is not None for item in items)
            else None
        ),
        "judge_legal_correctness_avg": _judge_dimension_average(items, "legal_correctness"),
        "judge_completeness_avg": _judge_dimension_average(items, "completeness"),
        "judge_gold_answer_alignment_avg": _judge_dimension_average(items, "gold_answer_alignment"),
        "judge_source_support_avg": _judge_dimension_average(items, "source_support"),
        "ragas_faithfulness_avg": _metric_average(items, "faithfulness", "ragas_metrics"),
        "ragas_context_precision_avg": _metric_average(items, "context_precision", "ragas_metrics"),
        "ragas_context_recall_avg": _metric_average(items, "context_recall", "ragas_metrics"),
        "stage_metrics": _aggregate_stage_metrics(items),
        "token_usage": _aggregate_token_usage(items),
    }


# Prix Scaleway Generative APIs (EUR / 1M tokens, input/output). La clé inclut
# le provider: un même nom de modèle peut avoir un tarif différent ailleurs.
# Albert est traité séparément comme gratuit dans l'agrégat.
_LLM_PRICE_EUR_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    ("scaleway", "qwen3-235b-a22b-instruct-2507"): (0.75, 2.25),
    ("scaleway", "mistral-medium-3.5-128b"): (1.50, 7.50),
    ("scaleway", "mistral-small-3.2-24b-instruct-2506"): (0.15, 0.35),
    ("scaleway", "llama-3.3-70b-instruct"): (0.90, 0.90),
    ("scaleway", "gpt-oss-120b"): (0.15, 0.60),
}


def _usage_cost_eur(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Coût EUR d'un usage, ou None sans tarif vérifié pour ce provider."""
    price = _LLM_PRICE_EUR_PER_MTOK.get(((provider or "").lower(), model or ""))
    if price is None:
        return None
    return round(prompt_tokens / 1_000_000 * price[0] + completion_tokens / 1_000_000 * price[1], 6)


class _TokenUsage:
    """Cumule les tokens facturables sur un client OpenAI-compatible.

    Le juge fait 1 appel; RAGAS en fait N (extraction de statements, NLI de
    faithfulness, context precision/recall) qui ré-envoient le contexte. On
    instrumente ``chat.completions.create`` pour tout capter sans dépendre des
    internes de RAGAS."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.attempted_calls = 0
        self.reported_cost = 0.0
        self.reported_cost_calls = 0

    def record(self, usage: Any) -> bool:
        if not usage:
            return False
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        self.calls += 1
        reported_cost = _safe_float(getattr(usage, "cost", None))
        if reported_cost is not None:
            self.reported_cost += reported_cost
            self.reported_cost_calls += 1
        return True

    def as_dict(self, model: str, provider: str, *, capture_complete: bool) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "calls": self.calls,
            "model": model,
            "provider": provider,
            "cost_eur": _usage_cost_eur(provider, model, self.prompt_tokens, self.completion_tokens),
            # OpenRouter reports this value in credits, not in EUR. Keep it
            # explicit and separate so it is never silently added to EUR.
            "reported_cost": round(self.reported_cost, 8) if self.reported_cost_calls else None,
            "reported_cost_unit": "openrouter_credit" if provider == "openrouter" and self.reported_cost_calls else None,
            "capture_complete": capture_complete,
        }


def _instrument_usage(client: Any, tracker: _TokenUsage) -> bool:
    """Instrumente ``client.chat.completions.create`` pour cumuler l'usage.

    On surcharge la méthode sur l'instance (``chat``/``completions`` sont des
    cached_property stables en openai v1) plutôt que d'envelopper le client:
    ``llm_factory`` de RAGAS vérifie le type, il faut garder l'instance réelle."""
    try:
        completions = client.chat.completions
        original = completions.create

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            tracker.attempted_calls += 1
            response = original(*args, **kwargs)
            try:
                tracker.record(getattr(response, "usage", None))
            except Exception:  # noqa: BLE001 — le tally ne doit jamais casser l'appel LLM
                pass
            return response

        completions.create = wrapped
        return True
    except Exception:  # noqa: BLE001 — si l'API change, on dégrade sans usage plutôt que crasher
        return False


def _aggregate_token_usage(items: list["EvalItem"]) -> dict[str, Any]:
    """Agrège l'usage LLM sans convertir un coût inconnu en faux zéro."""

    def _sum(getter: Any) -> dict[str, Any]:
        prompt = completion = calls = tracked = active = 0
        eur_total = 0.0
        eur_complete = True
        providers: set[str] = set()
        models: set[str] = set()
        reported_by_unit: dict[str, float] = {}
        reported_items = 0
        for item in items:
            result = getter(item)
            if not isinstance(result, dict) or result.get("status") == "skipped":
                continue
            active += 1
            usage = result.get("usage")
            if not isinstance(usage, dict):
                eur_complete = False
                continue
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            calls += int(usage.get("calls") or 0)
            if usage.get("capture_complete") is True:
                tracked += 1
            else:
                eur_complete = False
            if usage.get("provider"):
                providers.add(str(usage["provider"]))
            if usage.get("model"):
                models.add(str(usage["model"]))
            item_cost = _safe_float(usage.get("cost_eur"))
            if item_cost is None:
                eur_complete = False
            else:
                eur_total += item_cost
            reported_cost = _safe_float(usage.get("reported_cost"))
            reported_unit = str(usage.get("reported_cost_unit") or "")
            if reported_cost is not None and reported_unit:
                reported_by_unit[reported_unit] = reported_by_unit.get(reported_unit, 0.0) + reported_cost
                reported_items += 1
        reported_complete = active > 0 and tracked == active and reported_items == active and len(reported_by_unit) == 1
        single_reported_unit = next(iter(reported_by_unit)) if reported_complete else None
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "calls": calls,
            "items": tracked,
            "active_items": active,
            "coverage_complete": tracked == active,
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed" if providers else "",
            "model": next(iter(models)) if len(models) == 1 else "mixed" if models else "",
            "cost_eur": round(eur_total, 6) if active and eur_complete else 0.0 if not active else None,
            "reported_cost": round(reported_by_unit[single_reported_unit], 8) if single_reported_unit else None,
            "reported_cost_unit": single_reported_unit,
        }

    judge = _sum(lambda it: it.judge_result)
    ragas = _sum(lambda it: it.ragas_metrics)
    # Générateur/sélecteur = Albert (gratuit): estimation depuis les volumes stockés
    # réellement envoyés (sortie = compteur réel; input ~ chars/4).
    gen_out = sum(int((item.timing or {}).get("response_length_tokens") or 0) for item in items)
    gen_in_est = sum(int((item.metadata or {}).get("generator_prompt_chars") or 0) for item in items) // 4
    sel_in_est = sum(int((item.metadata or {}).get("selector_prompt_chars") or 0) for item in items) // 4
    sel_out_est = sum(int((item.metadata or {}).get("selector_response_chars") or 0) for item in items) // 4
    active_billable = [part for part in (judge, ragas) if part["active_items"]]
    billable = (
        round(sum(float(part["cost_eur"]) for part in active_billable), 4) if all(part["cost_eur"] is not None for part in active_billable) else None
    )
    return {
        "judge": judge,
        "ragas": ragas,
        "generator_albert_est": {"prompt_tokens": gen_in_est, "completion_tokens": gen_out, "cost_eur": 0.0},
        "selector_albert_est": {"prompt_tokens": sel_in_est, "completion_tokens": sel_out_est, "cost_eur": 0.0},
        "billable_cost_eur": billable,
        "note": (
            "Coût EUR nul uniquement si aucun appel payant n'est actif; sinon None dès qu'un tarif ou un relevé manque. "
            "Les crédits OpenRouter restent séparés. Albert est gratuit, tokens estimés (~4 chars/tok)."
        ),
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

    usage = _TokenUsage()
    instrumented = False
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
        instrumented = _instrument_usage(client, usage)  # capte tous les sous-appels RAGAS
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
            "usage": usage.as_dict(
                model,
                "scaleway",
                capture_complete=instrumented and usage.attempted_calls > 0 and usage.calls == usage.attempted_calls,
            ),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            # Les appels déjà terminés restent visibles, mais un run RAGAS
            # interrompu ne permet pas de garantir que tout usage est revenu.
            "usage": usage.as_dict(model, "scaleway", capture_complete=False),
        }


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

    # Les métriques de retrieval sont un DIAGNOSTIC, pas un verdict qualité:
    # arbitrage du 06/07/2026 (audit run 52) — le cap dur sur hit_rate=0
    # rendait le pass structurellement impossible pour 29/100 questions alors
    # que la réponse pouvait être parfaite (rationale positif, score 0.6), et
    # confondait artefacts d'ids gold avec échecs réels. Les deux caps
    # retrieval sont désormais soft: ils plafonnent le score stocké pour la
    # visibilité mais ne bloquent plus le pass; la part de hit_rate=0 est
    # agrégée séparément (retrieval_gap_rate).
    # ``doc_recall=None`` (gold set vide) => aucun cap retrieval.
    doc_recall = deterministic.get("doc_recall")
    hit_rate = deterministic.get("hit_rate")
    soft_caps: list[dict[str, Any]] = []
    if isinstance(doc_recall, int | float):
        if hit_rate == 0.0:
            soft_caps.append({"reason": "no_expected_source_retrieved", "max_score": rubric.no_expected_source_cap, "soft": True})
        elif doc_recall < 1.0:
            soft_caps.append({"reason": "missing_expected_source", "max_score": rubric.missing_expected_source_cap, "soft": True})

    # Le pass s'évalue sur le score AVANT caps soft: un cap soft (diagnostic
    # retrieval) plafonne le score stocké mais ne doit pas pouvoir faire
    # passer le score sous pass_min_score et bloquer le pass par ricochet.
    final_score = candidate_score
    if caps:
        final_score = min(final_score, *(float(cap["max_score"]) for cap in caps))
    pass_basis_score = min(1.0, max(0.0, final_score))
    if soft_caps:
        final_score = min(final_score, *(float(cap["max_score"]) for cap in soft_caps))
    final_score = min(1.0, max(0.0, final_score))

    failure_category = str(parsed.get("failure_category") or "none")
    pass_value = (
        pass_basis_score >= rubric.pass_min_score
        and not material_contradiction
        and not caps
        and all(normalized_dimensions[dim] >= floor for dim, floor in rubric.pass_dimension_floors.items())
    )

    parsed["raw_model_score"] = raw_model_score
    parsed["score"] = final_score
    # Persister la base du pass: le score stocké est plafonné par les caps soft
    # (ex. 0.6 sur hit_rate=0) alors que le pass s'évalue AVANT — sans ce champ,
    # une ligne pass=true/score=0.6 contredit pass_min_score=0.8 pour tout SQL
    # qui recalcule le pass depuis le score.
    parsed["pass_basis_score"] = pass_basis_score
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
    provider: str = DEFAULT_JUDGE_PROVIDER,
) -> dict[str, Any]:
    if not api_key:
        key_env = JUDGE_PROVIDERS.get(provider, {}).get("key_env", "OPENROUTER_API_KEY")
        return {"status": "skipped", "reason": f"missing judge API key ({key_env}) for provider '{provider}'"}
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
            "failure_category": "one of: none, wrong_law, incomplete, unsupported, hallucination, refusal, irrelevant, retrieval_gap",
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
        # Doctrine corpus réglementaire (audit du 06/07/2026): le juge évalue la
        # génération PAR RAPPORT AUX CONTEXTS SERVIS, jamais depuis son propre
        # savoir, et ne blâme pas la génération pour un trou de retrieval.
        "STRICT RULES — this is a regulatory corpus. "
        "(1) Judge ONLY from the gold answer and the retrieved_contexts provided; "
        "NEVER use your own legal knowledge to fill gaps or to fault the answer. "
        "(2) If the substance required by the gold answer is ABSENT from the retrieved_contexts, "
        "then a refusal or an explicit statement that the sources do not cover the point is the "
        "CORRECT candidate behavior: set failure_category='retrieval_gap', do not blame the answer, "
        "and never demand that the candidate infer or extrapolate beyond the contexts. "
        "(3) An answer that covers the gold answer's required points and whose additional statements "
        "are each anchored in the retrieved_contexts is NOT contradictory and NOT misaligned — "
        "anchored completeness is never a failure. "
        "(4) material_contradiction requires quoting the candidate sentence and the gold sentence "
        "that directly conflict; if you cannot quote both, it is not material. "
        "Return only valid JSON with keys: score, pass, failure_category, material_contradiction, "
        "dimensions, missing_required_points, contradictions, rationale, source_support. "
        "Two additional strict rules: (1) If the candidate answer states that the information was not found, "
        "is not specified in the sources, or cannot be determined, while the gold answer contains a substantive "
        "answer, this is ALWAYS a failure: set gold_answer_alignment and completeness to 0.0 and failure_category "
        "to retrieval_gap — an honest abstention is still a failed answer when the gold expects one. "
        "(2) Before reporting material_contradiction or contradictions, normalize units, periods and formulations "
        "(e.g. '25 jours ouvres' for a 5-day week EQUALS '5 semaines'; '6 semaines avant + 10 apres' EQUALS "
        "'16 semaines'): only report a contradiction when the facts are genuinely incompatible after normalization."
    )
    usage = _TokenUsage()
    usage_captured = False
    try:
        # base_url vide (ex. provider openai sans OPENAI_BASE_URL) -> ne pas la
        # passer, sinon OpenAI(base_url="") lève UnsupportedProtocol (revue #318).
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        create_kwargs: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        # DINUM (données réglementaires): sur OpenRouter, n'acheminer QU'AUX
        # endpoints Zero Data Retention. `data_collection: deny` exclut les
        # providers qui collectent/entraînent, mais la ZDR est un attribut
        # distinct chez OpenRouter -> `zdr: true` est requis en plus (revue
        # #329). Un modèle sans endpoint ZDR échoue -> inutilisable, voulu.
        if provider == "openrouter":
            create_kwargs["extra_body"] = {"provider": {"data_collection": "deny", "zdr": True}}
        response = client.chat.completions.create(**create_kwargs)
        usage_captured = usage.record(getattr(response, "usage", None))
        content = response.choices[0].message.content or "{}"
        parsed = _extract_json_object(content)
        parsed["status"] = "completed"
        calibrated = calibrate_judge_result(parsed, deterministic_metrics)
        calibrated["usage"] = usage.as_dict(model, provider, capture_complete=usage_captured)
        return calibrated
    except Exception as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            "usage": usage.as_dict(model, provider, capture_complete=usage_captured),
        }


def _combine_usage_payloads(results: list[dict[str, Any]], *, provider: str, model: str) -> dict[str, Any]:
    """Combine usage from every judge vote without hiding incomplete capture."""
    usages = [result.get("usage") for result in results]
    valid = [usage for usage in usages if isinstance(usage, dict)]
    providers = {str(usage.get("provider") or "") for usage in valid if usage.get("provider")}
    models = {str(usage.get("model") or "") for usage in valid if usage.get("model")}
    costs_eur = [_safe_float(usage.get("cost_eur")) for usage in valid]
    capture_complete = len(valid) == len(results) and all(usage.get("capture_complete") is True for usage in valid)

    reported = [
        (_safe_float(usage.get("reported_cost")), str(usage.get("reported_cost_unit") or ""))
        for usage in valid
        if usage.get("reported_cost") is not None
    ]
    reported_units = {unit for _, unit in reported if unit}
    reported_complete = len(reported) == len(valid) and len(reported_units) == 1 and all(cost is not None for cost, _ in reported)
    reported_unit = next(iter(reported_units)) if reported_complete else None

    return {
        "prompt_tokens": sum(int(usage.get("prompt_tokens") or 0) for usage in valid),
        "completion_tokens": sum(int(usage.get("completion_tokens") or 0) for usage in valid),
        "calls": sum(int(usage.get("calls") or 0) for usage in valid),
        "model": next(iter(models)) if len(models) == 1 else "mixed" if models else model,
        "provider": next(iter(providers)) if len(providers) == 1 else "mixed" if providers else provider,
        "cost_eur": round(sum(cost for cost in costs_eur if cost is not None), 6) if valid and all(cost is not None for cost in costs_eur) else None,
        "reported_cost": (round(sum(cost for cost, _ in reported if cost is not None), 8) if reported_complete else None),
        "reported_cost_unit": reported_unit,
        "capture_complete": capture_complete,
    }


def judge_answer_with_votes(*, votes: int = 1, **kwargs: Any) -> dict[str, Any]:
    """Vote majoritaire du juge : ``votes`` appels indépendants, verdict = majorité.

    Écrase mécaniquement le bruit propre du juge (single-shot mesuré le
    21-22/07 : scaleway/qwen3-235b 5,1 %, grok-4.5 6,1 % -> ~0,8-1,1 % en
    maj-3). Réservé au protocole OFFICIEL d'adoption ; le screening
    intermédiaire reste en single-shot. Les votes individuels sont archivés
    dans ``judge_result["votes"]`` (audit), le payload de base (rationale,
    catégorie) vient d'un vote MAJORITAIRE pour rester cohérent."""
    votes = normalize_judge_votes(votes)
    if votes == 1:
        return judge_answer(**kwargs)
    results = [judge_answer(**kwargs) for _ in range(votes)]
    combined_usage = _combine_usage_payloads(
        results,
        provider=str(kwargs.get("provider") or DEFAULT_JUDGE_PROVIDER),
        model=str(kwargs.get("model") or ""),
    )
    completed = [r for r in results if r.get("status") == "completed"]
    n_pass = sum(1 for r in completed if r.get("pass"))
    n_fail = len(completed) - n_pass
    quorum = votes // 2 + 1
    vote_audit = [
        {
            "pass": r.get("pass"),
            "score": r.get("score"),
            "failure_category": r.get("failure_category"),
            "status": r.get("status"),
            "reason": r.get("reason"),
            "usage": r.get("usage"),
        }
        for r in results
    ]
    if max(n_pass, n_fail) < quorum:
        return {
            "status": "failed",
            "reason": f"judge vote quorum not reached: required={quorum}, pass={n_pass}, fail={n_fail}, completed={len(completed)}/{votes}",
            "votes": vote_audit,
            "vote_agreement": f"{max(n_pass, n_fail)}/{len(completed)}",
            "usage": combined_usage,
        }
    verdict = n_pass >= quorum
    base = dict(next(r for r in completed if bool(r.get("pass")) == verdict))
    base["pass"] = verdict
    base["votes"] = vote_audit
    base["vote_agreement"] = f"{max(n_pass, n_fail)}/{len(completed)}"
    base["usage"] = combined_usage
    return base


def build_full_ministry_scope() -> Any:
    """Scope « utilisateur pleinement granté »: tous les ministères du catalog
    + les tables partagées.

    L'app route les corpus ministériels par RetrievalScope construit à chaque
    requête selon les droits du groupe (jamais via v3_tables — question de
    droits, pas de config globale). Sans scope, l'eval retombait sur v3_tables
    et les corpus mso/mi/masa étaient invisibles: les questions MSO
    plafonnaient à 0.25 uniquement pour ça (constat du 06/07/2026)."""
    from assistant_rh_rag_pipeline.ministry_scope import MINISTRY_CATALOG, SHARED_TABLE_KEYS, RetrievalScope

    ministry_keys = tuple(ministry.table_key for ministry in MINISTRY_CATALOG.values())
    return RetrievalScope(selected_ministry="eval_all_ministries", table_keys=(*ministry_keys, *SHARED_TABLE_KEYS))


def resolve_question_scope(question: GoldsetQuestion, ministry_scope_mode: str) -> Any:
    """Scope de retrieval d'une question selon le mode choisi.

    'per-question' (recommandé): une question MATTE/MSO/MI/MASA est évaluée
    dans le scope de SON ministère + tables partagées — comme un agent de ce
    ministère dans l'app. Sonde du 06/07/2026 en scope « all »: contamination
    inter-ministères (question MSO répondue depuis un mode opératoire MASA,
    questions MATTE depuis le Vademecum MSO) — aucun utilisateur réel n'a ce
    scope-là.

    Les questions NON ministérielles (Service-Public, manual, synthetic,
    DGAFP) suivent aussi le **parcours MATTE** (matte + tables partagées SP +
    Légifrance) — décision Paul 06/07/2026. Le goldset est construit en
    contexte MATTE/DGAFP et 55/68 de ces questions ont leur gold dans SP ou
    Légifrance (couvert par les tables partagées, présentes dans TOUT scope
    ministériel). Les évaluer en scope complet leur infligeait le pool le plus
    bruité (chunks mso/mi/masa hors-sujet) sans qu'aucun utilisateur réel n'ait
    ce scope — un agent DGAFP/MATTE n'interroge jamais tous les ministères.
    """
    if ministry_scope_mode == "none":
        return None
    if ministry_scope_mode == "per-question":
        from assistant_rh_rag_pipeline.ministry_scope import MINISTRY_CATALOG, build_retrieval_scope

        ministry_id = (question.source or "").strip().lower()
        # Sources non ministérielles (manual/Service-Public/synthetic/DGAFP)
        # -> parcours MATTE (matte + SP + Légifrance), pas scope complet.
        if ministry_id not in MINISTRY_CATALOG:
            ministry_id = "matte"
        return build_retrieval_scope(ministry_id)
    return build_full_ministry_scope()


def run_question(
    *,
    pipe: Any,
    question: GoldsetQuestion,
    identifier_aliases: dict[str, set[str]] | None = None,
    run_ragas: bool,
    run_judge: bool,
    judge_model: str,
    judge_base_url: str,
    judge_api_key: str,
    judge_provider: str = DEFAULT_JUDGE_PROVIDER,
    judge_votes: int = 1,
    ragas_model: str,
    scaleway_base_url: str,
    scaleway_api_key: str,
    retrieval_scope: Any = None,
) -> EvalItem:
    started = time.perf_counter()
    item = EvalItem(
        question_id=question.id,
        question=question.question,
        gold_answer=question.gold_answer,
        gold_sources=question.gold_sources,
    )
    try:
        result = pipe.run_with_trace(question.question, retrieval_scope=retrieval_scope)
        elapsed_ms = (time.perf_counter() - started) * 1000
        contexts = context_payload(result)
        doc_ids = retrieved_doc_ids(result, contexts)

        item.answer = result.answer
        item.contexts = contexts
        item.sources = result.sources
        item.timing = {**result.timing, "eval_total_ms": elapsed_ms}
        metadata = dict(result.metadata) if isinstance(result.metadata, dict) else {}
        # Les prompts exposés par Pipeline sont exactement les deux messages
        # envoyés au générateur. Conserver uniquement leurs tailles évite
        # d'écrire le contenu sensible tout en donnant une estimation fidèle.
        generator_user_prompt = str(getattr(pipe, "last_full_prompt", "") or "")
        generator_system_prompt = str(getattr(pipe, "last_system_prompt", "") or "")
        metadata["generator_prompt_chars"] = len(generator_user_prompt) + len(generator_system_prompt)
        item.metadata = metadata
        item.deterministic_metrics = deterministic_metrics(question.retrieval_gold, doc_ids, aliases=identifier_aliases)
        item.deterministic_metrics["stages"] = stage_retrieval_metrics(metadata, question.retrieval_gold, aliases=identifier_aliases)

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
            item.judge_result = judge_answer_with_votes(
                votes=judge_votes,
                question=question.question,
                gold_answer=question.gold_answer,
                answer=result.answer,
                contexts=context_texts,
                deterministic_metrics=item.deterministic_metrics,
                model=judge_model,
                base_url=judge_base_url,
                api_key=judge_api_key,
                provider=judge_provider,
            )
        else:
            item.judge_result = {"status": "skipped", "reason": "disabled"}
    except psycopg.errors.QueryCanceled as exc:
        # Sous-classe d'OperationalError MAIS annulation/timeout d'une requête
        # = propre à la question, pas une coupure de connexion : absorbée dans
        # item.error pour que le run continue (revue #331 — sinon le retry la
        # rejouait 3x puis tuait le run entier).
        item.error = str(exc)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # Coupure de connexion DB = transitoire : doit remonter à la boucle de
        # retry du runner au lieu d'être absorbée dans item.error (revue #329 —
        # sinon le retry ne voit jamais l'exception et la question est perdue).
        raise
    except Exception as exc:
        item.error = str(exc)
    return item


def run_question_with_retry(*, attempts: int = 3, backoff_s: float = 5.0, **kwargs: Any) -> EvalItem:
    """Rejoue la question sur coupure de connexion DB. Les composants du
    pipeline ouvrent une connexion par requête : rejouer suffit, rien à
    reconstruire."""
    for attempt in range(1, attempts + 1):
        try:
            return run_question(**kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if attempt == attempts:
                raise
            print(
                f"[resilience] connexion DB perdue sur la question {kwargs['question'].id} (tentative {attempt}/{attempts}) : {exc}",
                flush=True,
            )
            time.sleep(backoff_s * attempt)
    raise AssertionError("unreachable")


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
                    # TODO(pr212-review): always blank — item.metadata is the pipeline
                    # result metadata, which has no "theme"; the goldset theme
                    # (GoldsetQuestion.theme) is never copied onto EvalItem. Either
                    # thread the goldset theme through or drop this column.
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
    parser.add_argument(
        "--selector-model",
        default="",
        help="Override du modèle du sélecteur (ex: mistral-medium-2508) sans toucher à la config runtime partagée.",
    )
    parser.add_argument(
        "--generator-model",
        default="",
        help="Override du modèle générateur (ex: deepseek-v4-flash) sans toucher à la config runtime partagée.",
    )
    parser.add_argument(
        "--system-prompt-name",
        default="",
        help="Override du system prompt générateur (nom dans system_prompts) sans toucher à la config runtime partagée.",
    )
    parser.add_argument(
        "--section-rerank-top-k",
        type=int,
        default=None,
        help="Override du nombre de sections offertes au sélecteur (v3_rerank_top_k runtime sinon).",
    )
    parser.add_argument(
        "--rerank-input-k",
        type=int,
        default=None,
        help="Override de l'ENTRÉE du reranker (candidats vus ; v3_rerank_input_k runtime sinon). A/B vague 1 : 40.",
    )
    parser.add_argument("--initial-top-k", type=int, default=None, help="Override retrieval.initial_top_k (ablation).")
    parser.add_argument("--ivfflat-probes", type=int, default=None, help="Override retrieval.ivfflat_probes (ablation; 0=défaut serveur).")
    parser.add_argument("--min-kept-sections", type=int, default=None, help="Override selector.min_kept_sections (ablation; 0=désactivé).")
    parser.add_argument("--doc-entire-threshold-wide", type=int, default=None, help="Override context.doc_entire_threshold_wide (ablation).")
    parser.add_argument(
        "--ministry-scope",
        choices=["per-question", "all", "none"],
        default="per-question",
        help=(
            "Scope ministériel du retrieval. 'per-question' (défaut) = les questions MSO/MI/MASA "
            "sont évaluées dans le scope de LEUR ministère (comme un agent de ce ministère dans l'app); "
            "toutes les autres (MATTE, manual, Service-Public, synthetic, DGAFP) suivent le parcours MATTE "
            "(matte + tables partagées SP/Légifrance), PAS le scope complet; 'all' = utilisateur "
            "pleinement granté partout (contamination inter-ministères possible); 'none' = comportement "
            "historique (v3_tables runtime seulement, mso/mi/masa invisibles)."
        ),
    )
    parser.add_argument(
        "--judge-provider",
        default=os.getenv("JUDGE_PROVIDER", DEFAULT_JUDGE_PROVIDER),
        choices=sorted(JUDGE_PROVIDERS),
        help="LLM judge provider (pilote la clé ET la base URL; tracé en base).",
    )
    parser.add_argument(
        "--judge-model",
        default="",
        help="Judge model override. Empty selects the configured default for --judge-provider.",
    )
    parser.add_argument(
        "--judge-base-url",
        default=None,
        help="Override de la base URL du juge (sinon dérivée du provider: var d'env puis défaut).",
    )
    parser.add_argument(
        "--judge-votes",
        type=int,
        default=os.getenv("JUDGE_VOTES", "1") or "1",
        help=(
            "Vote majoritaire du juge : N appels par réponse, verdict = majorité. "
            "Protocole OFFICIEL d'adoption (gates staging/prod) : 3 votes sur le juge "
            "souverain Scaleway (bruit propre 5,1%% single-shot -> ~0,8%% en maj-3). "
            "Screening intermédiaire : 1 (défaut)."
        ),
    )
    parser.add_argument(
        "--ragas-model",
        default=os.getenv("RAGAS_MODEL", DEFAULT_RAGAS_MODEL),
        help="Scaleway model for RAGAS metrics (fast instruct model; separate from the judge).",
    )
    parser.add_argument(
        "--scaleway-base-url", default=os.getenv("SCALEWAY_BASE_URL", DEFAULT_SCALEWAY_BASE_URL), help="OpenAI-compatible base URL (RAGAS/Scaleway)."
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first item failure.")
    parser.add_argument("--baseline-run-id", type=int, default=None, help="Recorded rag_quality_eval_runs.id to compare against.")
    parser.add_argument("--baseline-run-label", default="", help="Recorded rag_quality_eval_runs.run_label to compare against.")
    parser.add_argument("--require-baseline", action="store_true", help="Fail when the baseline run is missing or not comparable.")
    parser.add_argument("--gate-against-baseline", action="store_true", help="Fail when baseline comparison metrics regress beyond thresholds.")
    parser.add_argument("--max-judge-pass-rate-drop", type=float, default=0.05, help="Maximum allowed judge_pass_rate drop versus baseline.")
    parser.add_argument("--max-doc-recall-drop", type=float, default=0.05, help="Maximum allowed doc_recall_avg drop versus baseline.")
    return parser


def derive_completion_status(items: list[EvalItem], *, judge_enabled: bool, ragas_enabled: bool, judge_votes: int = 1) -> tuple[str, str]:
    """Derive the run status from executed items (before any baseline gating).

    Returns ``(status, error)``. Rules, strictest last:
    - some items errored -> ``completed_with_errors``;
    - *every* item errored (pipeline/DB/model misconfiguration) -> ``failed``,
      otherwise the eval would report success and CI stay green while nothing ran;
    - a judge/RAGAS sub-task requested but ``failed`` on every *executed* item is a
      configuration failure -> ``failed`` (``skipped`` sub-results are intentional).
    """
    status = "completed"
    error = ""
    if any(item.error for item in items):
        status = "completed_with_errors"
    if items and all(item.error for item in items):
        return "failed", f"all {len(items)} questions failed to execute"
    if judge_enabled and judge_votes > 1:
        completed_judgments = sum(1 for item in items if not item.error and item.judge_result.get("status") == "completed")
        if completed_judgments != len(items):
            return "failed", f"official judge protocol completed {completed_judgments}/{len(items)} judgments"
    for label, enabled, attr in (
        ("judge", judge_enabled, "judge_result"),
        ("ragas", ragas_enabled, "ragas_metrics"),
    ):
        if not enabled:
            continue
        sub_statuses = [getattr(item, attr).get("status") for item in items if not item.error]
        if sub_statuses and all(sub_status == "failed" for sub_status in sub_statuses):
            status = "failed"
            error = error or f"{label} requested but failed on all {len(sub_statuses)} evaluated items"
    return status, error


def run_eval(args: argparse.Namespace) -> EvalSummary:
    if args.any_goldset and not args.tag:
        raise ValueError("At least one --tag is required with --any-goldset.")
    if not args.skip_judge:
        args.judge_votes = normalize_judge_votes(args.judge_votes)
        args.judge_model = resolve_judge_model(args.judge_provider, args.judge_model)

    dsn = resolve_dsn(args.dsn, args.dsn_env)
    # The pipeline and prompt/config helpers read the canonical runtime DSN.
    # Bind the explicitly selected eval target for this process so staging
    # evals do not accidentally fetch prompts/config from a local Supabase DSN.
    os.environ["SCW_POSTGRES_DSN"] = dsn
    runtime_config = get_rag_config()
    pipeline_config = runtime_config_to_rag_config(runtime_config)
    config_adjustments: list[str] = []
    # Overrides d'expérimentation A/B: la config runtime (rag_config, ligne
    # unique en base) est PARTAGÉE — deux runs parallèles avec des réglages
    # différents doivent surcharger localement, pas muter la base. Appliqués
    # AVANT le fingerprint pour que la clé de dédup/comparabilité les voie.
    if args.selector_model:
        pipeline_config.selector.model = args.selector_model
        config_adjustments.append(f"selector_model={args.selector_model}")
    if args.generator_model:
        pipeline_config.generation.model = args.generator_model
        config_adjustments.append(f"generator_model={args.generator_model}")
    if args.system_prompt_name:
        pipeline_config.generation.system_prompt_name = args.system_prompt_name
        config_adjustments.append(f"system_prompt_name={args.system_prompt_name}")
    if args.section_rerank_top_k is not None:
        pipeline_config.aggregation.section_rerank_top_k = args.section_rerank_top_k
        config_adjustments.append(f"section_rerank_top_k={args.section_rerank_top_k}")
    if args.rerank_input_k is not None:
        pipeline_config.aggregation.rerank_input_k = args.rerank_input_k
        config_adjustments.append(f"rerank_input_k={args.rerank_input_k}")
    if args.initial_top_k is not None:
        pipeline_config.retrieval.initial_top_k = args.initial_top_k
        config_adjustments.append(f"initial_top_k={args.initial_top_k}")
    if args.ivfflat_probes is not None:
        pipeline_config.retrieval.ivfflat_probes = args.ivfflat_probes
        config_adjustments.append(f"ivfflat_probes={args.ivfflat_probes}")
    if args.min_kept_sections is not None:
        pipeline_config.selector.min_kept_sections = args.min_kept_sections
        config_adjustments.append(f"min_kept_sections={args.min_kept_sections}")
    if args.doc_entire_threshold_wide is not None:
        pipeline_config.context.doc_entire_threshold_wide = args.doc_entire_threshold_wide
        config_adjustments.append(f"doc_entire_threshold_wide={args.doc_entire_threshold_wide}")
    config_hash = config_fingerprint(pipeline_config)
    git_sha = _git_sha()
    run_label = args.run_label or f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}_{args.goldset_name}"
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / args.goldset_name / run_label)

    json_path, csv_path = artifact_paths(output_dir, run_label)

    questions = load_goldset_questions(dsn, goldset_name=args.goldset_name, tags=args.tag, limit=args.limit, any_goldset=args.any_goldset)
    if not questions:
        raise RuntimeError(f"No questions found for goldset={args.goldset_name!r}, tags={args.tag!r}.")
    gold_id_maps = load_gold_id_maps(dsn)
    if any(gold_id_maps.get(key) for key in ("doc_short", "matte_short", "article", "legal_ref")):
        for question in questions:
            question.gold_doc_ids = merge_gold_doc_ids(question.gold_doc_ids, question.gold_sources, gold_id_maps)
    identifier_aliases = gold_id_maps.get("aliases", {})
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
                if baseline_comparison_requested(args):
                    comparison = build_baseline_comparison(
                        conn,
                        args=args,
                        candidate_aggregate=_json_dict(existing_run.get("aggregate")),
                        goldset_name=args.goldset_name,
                        tags=args.tag,
                        eval_scope=eval_scope,
                    )
                    summary["baseline_comparison"] = comparison
                    if baseline_gate_failed(args, comparison):
                        summary["status"] = "failed_quality_gate"
                json_path, csv_path = write_artifacts(output_dir, run_label, [], summary)
                return EvalSummary(
                    run_id=None,
                    status=summary["status"],
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
                    judge_provider=args.judge_provider if not args.skip_judge else "",
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
    api_key = os.getenv("SCALEWAY_API_KEY", "").strip()  # RAGAS (Scaleway)
    # Le provider pilote réellement la clé ET la base URL du juge (revue #318):
    # --judge-provider scaleway utilise la clé/endpoint Scaleway, openrouter les
    # siens — jamais un provider inscrit avec la clé d'un autre.
    judge_provider, judge_base_url, judge_api_key = resolve_judge_endpoint(args.judge_provider, args.judge_base_url)
    items: list[EvalItem] = []
    status = "completed"
    error = ""

    item_conn: psycopg.Connection[Any] | None = None
    try:
        if run_id is not None:
            item_conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
        for question in questions:
            # Un run complet tient ~1 h : le serveur staging peut couper les
            # connexions en cours de route (backends tués — runs 119/120 du
            # 21/07 morts à 3 et 9 items). run_question RE-LÈVE les erreurs de
            # connexion psycopg (au lieu de les absorber dans item.error) pour
            # que ce retry les voie.
            item = run_question_with_retry(
                pipe=pipe,
                question=question,
                identifier_aliases=identifier_aliases,
                run_ragas=not args.skip_ragas,
                run_judge=not args.skip_judge,
                judge_model=args.judge_model,
                judge_base_url=judge_base_url,
                judge_api_key=judge_api_key,
                judge_provider=judge_provider,
                judge_votes=args.judge_votes,
                ragas_model=args.ragas_model,
                scaleway_base_url=args.scaleway_base_url,
                scaleway_api_key=api_key,
                retrieval_scope=resolve_question_scope(question, args.ministry_scope),
            )
            items.append(item)
            if item_conn is not None and run_id is not None:
                for attempt in range(1, 4):
                    try:
                        # Reconnexion DANS le try : un « connection refused »
                        # pendant la reprise consomme une tentative au lieu de
                        # s'échapper de la boucle.
                        if item_conn.closed:
                            item_conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
                        if attempt > 1:
                            # Un COMMIT peut être appliqué côté serveur sans
                            # acquittement (connexion morte entre les deux) :
                            # purge préalable, dans la même transaction que la
                            # réinsertion, pour ne jamais dupliquer l'item —
                            # la table n'a pas d'UNIQUE (run_id, question_id).
                            item_conn.execute(
                                "DELETE FROM public.rag_quality_eval_items WHERE run_id = %s AND question_id = %s",
                                (run_id, question.id),
                            )
                        insert_eval_item(item_conn, run_id, item)
                        item_conn.commit()
                        break
                    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                        if attempt == 3:
                            raise
                        print(
                            f"[resilience] connexion d'insertion perdue (tentative {attempt}/3) : {exc} — reconnexion",
                            flush=True,
                        )
                        try:
                            item_conn.close()
                        except Exception:
                            pass
                        time.sleep(5 * attempt)
            if item.error and args.fail_fast:
                raise RuntimeError(item.error)
        status, status_error = derive_completion_status(
            items,
            judge_enabled=not args.skip_judge,
            ragas_enabled=not args.skip_ragas,
            judge_votes=args.judge_votes if not args.skip_judge else 0,
        )
        error = error or status_error
    except Exception as exc:
        status = "failed"
        error = str(exc)
        if run_id is None:
            raise
    finally:
        if item_conn is not None:
            item_conn.close()
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
        if baseline_comparison_requested(args):
            with psycopg.connect(dsn, row_factory=dict_row) as conn:
                comparison = build_baseline_comparison(
                    conn,
                    args=args,
                    candidate_aggregate=aggregate,
                    goldset_name=args.goldset_name,
                    tags=args.tag,
                    eval_scope=eval_scope,
                )
            aggregate["baseline_comparison"] = comparison
            if baseline_gate_failed(args, comparison):
                status = "failed_quality_gate"
                error = error or "; ".join(comparison.get("failures") or ["baseline quality gate failed"])
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
    return 0 if summary.status not in {"failed", "failed_quality_gate"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
