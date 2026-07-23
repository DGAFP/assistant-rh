#!/usr/bin/env python3
"""Calibrate the RAG quality judge against human PASS/BLOCKS labels.

Given a labelled CSV of historical answers with reviewer verdicts (columns:
``question, answer, gold_answer, verdict`` where verdict is ``PASS``/``BLOCKS``),
this runs the LLM-as-judge on each answer against its gold answer and reports how
well the calibrated ``pass`` decision agrees with the human verdict.

The judge is run once per example and cached (``--cache``); the confusion matrix
and the optional ``--sweep`` over rubric thresholds then run offline against the
cache, so tuning never re-bills the model.

Because no retrieval contexts exist for historical answers, the gold answer is
passed as the sole context (source_support is measured against the reference) and
``deterministic_metrics`` is empty (retrieval caps are disabled — those are
validated separately in the live pipeline). The key quantity to protect is
``false_pass``: the judge passing an answer a human blocked.

Usage:
    uv run python scripts/calibrate_judge.py \\
        --labels data/eval/judge_calibration/labels.csv --cache .cache/judge_calibration.json
    uv run python scripts/calibrate_judge.py --labels ... --cache ... --sweep
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.goldset.eval import (  # noqa: E402
    DEFAULT_JUDGE_MODEL,
    DEFAULT_JUDGE_PROVIDER,
    DEFAULT_JUDGE_RUBRIC,
    JudgeRubric,
    calibrate_judge_result,
    judge_answer,
    resolve_judge_endpoint,
)


def load_labels(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle)]
    usable = []
    for row in rows:
        verdict = (row.get("verdict") or "").strip().upper()
        if verdict not in {"PASS", "BLOCKS"}:
            continue
        if not (row.get("question") or "").strip() or not (row.get("answer") or "").strip() or not (row.get("gold_answer") or "").strip():
            continue
        usable.append(row)
    return usable


def _cache_fingerprint(labels: list[dict], provider: str, model: str, base_url: str) -> str:
    """Fingerprint the calibration inputs so a stale cache is not reused.

    Keyed on the judge provider/endpoint/model and the label fields that drive
    the judge (question/answer/gold_answer/verdict). Editing labels.csv or
    switching provider/base URL/model changes the fingerprint, forcing a re-run
    instead of silently reporting a confusion matrix computed against different
    inputs (revue #318: le provider et l'endpoint entrent dans l'empreinte).
    """
    payload = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "labels": [{key: (row.get(key) or "") for key in ("question", "answer", "gold_answer", "verdict")} for row in labels],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def capture_judge(labels: list[dict], cache_path: Path) -> list[dict]:
    """Run the judge once per labelled answer and cache the raw dimensions."""
    # Même juge que le harnais d'éval: provider (défaut OpenRouter) pilote la
    # clé ET la base URL; le modèle vient de OPENROUTER_JUDGE_MODEL sinon défaut.
    provider = os.getenv("JUDGE_PROVIDER", DEFAULT_JUDGE_PROVIDER).strip()
    model = os.getenv("OPENROUTER_JUDGE_MODEL", DEFAULT_JUDGE_MODEL).strip()
    provider, base_url, api_key = resolve_judge_endpoint(provider)
    fingerprint = _cache_fingerprint(labels, provider, model, base_url)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            rows = cached.get("rows", [])
            print(f"using cached judge outputs: {cache_path} ({len(rows)} rows)", file=sys.stderr)
            return rows
        print(f"cache {cache_path} is stale (labels, provider or model changed) — re-running judge", file=sys.stderr)

    if not api_key:
        raise SystemExit(f"clé API du juge requise pour le provider '{provider}' (définis-la dans .env).")
    print(f"judge: {provider}/{model} @ {base_url} — {len(labels)} examples", file=sys.stderr)

    captured = []
    for i, row in enumerate(labels, start=1):
        result = judge_answer(
            question=row["question"],
            gold_answer=row["gold_answer"],
            answer=row["answer"],
            contexts=[row["gold_answer"]],
            deterministic_metrics={},
            model=model,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
        )
        captured.append(
            {
                "question": row["question"],
                "verdict": row["verdict"].strip().upper(),
                "category": row.get("category", ""),
                "raw": {
                    "dimensions": result.get("dimensions", {}),
                    "score": result.get("raw_model_score"),
                    "material_contradiction": result.get("material_contradiction"),
                    "failure_category": result.get("failure_category"),
                    "status": result.get("status"),
                },
            }
        )
        print(f"  [{i}/{len(labels)}] {row['verdict']:6s} {row['question'][:60]}", file=sys.stderr)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"fingerprint": fingerprint, "rows": captured}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote cache {cache_path}", file=sys.stderr)
    return captured


def confusion(captured: list[dict], rubric: JudgeRubric) -> dict:
    """Confusion matrix with human BLOCKS as the positive class."""
    counts = {"true_block": 0, "false_pass": 0, "false_block": 0, "true_pass": 0}
    disagreements = []
    for row in captured:
        raw = row["raw"]
        parsed = {
            "dimensions": dict(raw["dimensions"]),
            "score": raw["score"],
            "material_contradiction": raw["material_contradiction"],
            "failure_category": raw.get("failure_category"),
        }
        judged = calibrate_judge_result(parsed, {}, rubric)
        judge_block = not judged["pass"]
        human_block = row["verdict"] == "BLOCKS"
        if human_block and judge_block:
            counts["true_block"] += 1
        elif human_block and not judge_block:
            counts["false_pass"] += 1
            disagreements.append(("FALSE-PASS", row, judged))
        elif not human_block and judge_block:
            counts["false_block"] += 1
            disagreements.append(("false-block", row, judged))
        else:
            counts["true_pass"] += 1
    counts["agreement"] = (counts["true_block"] + counts["true_pass"]) / max(1, len(captured))
    return {"counts": counts, "disagreements": disagreements, "total": len(captured)}


def print_report(captured: list[dict], rubric: JudgeRubric) -> None:
    report = confusion(captured, rubric)
    c = report["counts"]
    print(
        f"agreement={c['agreement']:.0%}  "
        f"true_block={c['true_block']} false_pass(DANGER)={c['false_pass']} "
        f"false_block={c['false_block']} true_pass={c['true_pass']}  (n={report['total']})"
    )
    for kind, row, judged in report["disagreements"]:
        dims = {k: round(v, 2) for k, v in judged["dimensions"].items()}
        caps = [cap["reason"] for cap in judged["calibration_caps"]]
        print(f"  {kind}: score={judged['score']:.2f} {dims} caps={caps}")
        print(f"     {row['verdict']:6s} | {row['category']} | {row['question'][:70]}")


def sweep(captured: list[dict]) -> None:
    """Grid-search pass thresholds; rank by (fewest false_pass, most agreement)."""
    if not captured:
        print("No captured examples to sweep.", file=sys.stderr)
        return
    best = None
    for pmin in (0.6, 0.65, 0.7, 0.75, 0.8):
        for legal in (0.6, 0.7, 0.75, 0.8):
            for comp in (0.5, 0.6, 0.7, 0.75):
                for gold in (0.6, 0.7, 0.75, 0.8):
                    rubric = replace(
                        DEFAULT_JUDGE_RUBRIC,
                        pass_min_score=pmin,
                        pass_dimension_floors={
                            "legal_correctness": legal,
                            "completeness": comp,
                            "gold_answer_alignment": gold,
                        },
                    )
                    c = confusion(captured, rubric)["counts"]
                    key = (c["false_pass"], -(c["true_block"] + c["true_pass"]))
                    if best is None or key < best[0]:
                        best = (key, dict(pmin=pmin, legal=legal, comp=comp, gold=gold), c)
    _, params, c = best
    print("\nbest pass-gate (min false_pass, then max agreement):", params)
    print(
        f"  agreement={c['agreement']:.0%} true_block={c['true_block']} "
        f"false_pass={c['false_pass']} false_block={c['false_block']} true_pass={c['true_pass']}"
    )
    print("  NOTE: a tiny labelled set overfits; never adopt a setting that raises false_pass.")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=REPO_ROOT / "data/eval/judge_calibration/labels.csv")
    parser.add_argument("--cache", type=Path, default=REPO_ROOT / ".cache/assistant-rh/judge_calibration.json")
    parser.add_argument("--sweep", action="store_true", help="Grid-search pass thresholds against the cache.")
    args = parser.parse_args(argv)

    if not args.labels.exists():
        print(f"Error: labels file not found at {args.labels}", file=sys.stderr)
        return 1
    labels = load_labels(args.labels)
    print(f"usable labelled examples (PASS/BLOCKS + answer + gold_answer): {len(labels)}", file=sys.stderr)
    if not labels:
        print("Error: no usable labelled examples found.", file=sys.stderr)
        return 1
    captured = capture_judge(labels, args.cache)

    print("\n=== current rubric ===")
    print_report(captured, DEFAULT_JUDGE_RUBRIC)
    if args.sweep:
        sweep(captured)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
