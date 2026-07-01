#!/usr/bin/env python3
"""Backfill RAGAS metrics onto an already-recorded eval run.

RAGAS is slow (~45s/question of sequential sub-calls), so a large run is best
done judge-only first, then enriched here from the stored answers/contexts — no
pipeline re-run. Resumable: items whose ragas_metrics already completed are
skipped, so it can be re-launched after an interruption and continue.

Usage:
    uv run python scripts/backfill_ragas.py --run-label baseline_v1_canonical_20260629 \\
        --dsn-env SCW_POSTGRES_DSN_STAGING
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.goldset.eval import (  # noqa: E402
    DEFAULT_RAGAS_MODEL,
    DEFAULT_SCALEWAY_BASE_URL,
    aggregate_items,
    compute_ragas_metrics,
    resolve_dsn,
)


def _context_texts(contexts: list[dict]) -> list[str]:
    return [str(c.get("content") or "") for c in (contexts or []) if str(c.get("content") or "").strip()]


def summarize_ragas_status(rows: list[dict]) -> tuple[str, dict[str, int]]:
    counts = {"completed": 0, "failed": 0, "skipped": 0, "pending": 0}
    for row in rows:
        if not str(row.get("answer") or "").strip():
            continue
        metrics = row.get("ragas_metrics") or {}
        status = metrics.get("status") if isinstance(metrics, dict) else None
        if status in counts:
            counts[status] += 1
        else:
            counts["pending"] += 1

    total = sum(counts.values())
    if total == 0:
        return "skipped", counts
    if counts["completed"] == total:
        return "completed", counts
    if counts["failed"] == total:
        return "failed", counts
    if counts["completed"] == 0 and counts["skipped"] == total:
        return "skipped", counts
    return "partial", counts


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", default=None)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--ragas-model", default=os.getenv("RAGAS_MODEL", DEFAULT_RAGAS_MODEL))
    args = parser.parse_args(argv)

    dsn = resolve_dsn(args.dsn, args.dsn_env)
    api_key = os.getenv("SCALEWAY_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SCALEWAY_API_KEY is required.")
    base_url = os.getenv("SCALEWAY_BASE_URL", DEFAULT_SCALEWAY_BASE_URL).strip()

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        if args.run_id:
            run_id = args.run_id
        else:
            row = conn.execute(
                "SELECT id FROM public.rag_quality_eval_runs WHERE run_label = %s ORDER BY id DESC LIMIT 1", (args.run_label,)
            ).fetchone()
            if not row:
                raise SystemExit(f"no run found for run_label={args.run_label!r}")
            run_id = row["id"]

        todo = conn.execute(
            """
            SELECT id, question, gold_answer, answer, contexts
            FROM public.rag_quality_eval_items
            WHERE run_id = %s AND COALESCE(ragas_metrics->>'status', '') <> 'completed'
              AND answer IS NOT NULL AND btrim(answer) <> ''
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        print(f"run {run_id}: {len(todo)} items need RAGAS (model {args.ragas_model})", file=sys.stderr)

        done = 0
        for i, item in enumerate(todo, start=1):
            metrics = compute_ragas_metrics(
                question=item["question"],
                answer=item["answer"],
                contexts=_context_texts(item["contexts"]),
                reference=item["gold_answer"] or "",
                model=args.ragas_model,
                base_url=base_url,
                api_key=api_key,
            )
            conn.execute(
                "UPDATE public.rag_quality_eval_items SET ragas_metrics = %s WHERE id = %s",
                (json.dumps(metrics, ensure_ascii=False), item["id"]),
            )
            done += 1 if metrics.get("status") == "completed" else 0
            print(f"  [{i}/{len(todo)}] {metrics.get('status')} {item['question'][:55]}", file=sys.stderr)

        # Refresh the run aggregate's RAGAS averages from all items.
        rows = conn.execute(
            "SELECT answer, deterministic_metrics, ragas_metrics, judge_result, error FROM public.rag_quality_eval_items WHERE run_id = %s",
            (run_id,),
        ).fetchall()
        from src.goldset.eval import EvalItem  # noqa: E402

        items = [
            EvalItem(
                question_id=0,
                question="",
                gold_answer="",
                gold_sources=[],
                deterministic_metrics=r["deterministic_metrics"] or {},
                ragas_metrics=r["ragas_metrics"] or {},
                judge_result=r["judge_result"] or {},
                error=r["error"] or "",
            )
            for r in rows
        ]
        agg = aggregate_items(items)
        ragas_keys = {k: agg[k] for k in ("ragas_faithfulness_avg", "ragas_context_precision_avg", "ragas_context_recall_avg")}
        ragas_status, status_counts = summarize_ragas_status(rows)
        ragas_keys["ragas_status_counts"] = status_counts
        conn.execute(
            "UPDATE public.rag_quality_eval_runs SET aggregate = aggregate || %s::jsonb, ragas_status = %s WHERE id = %s",
            (json.dumps(ragas_keys), ragas_status, run_id),
        )
        print(f"completed RAGAS on {done}/{len(todo)} new items; status={ragas_status} counts={status_counts}", file=sys.stderr)
    return 1 if ragas_status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
