"""Rejudge the frozen M1/M0a answers locally with verified quotations; never regenerate answers.

The original 28–29 September campaign and its superseded item remain untouched.
Use --prepare-only to inspect the frozen input manifest before any paid call.
Journal the label/protocol before launching. Each paid result is checkpointed privately.
"""

import argparse
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from scripts.conformance.m1_live import ROOT, load_environment, source_fingerprint

COMPONENTS = {"m0a": [240], "legacy": [246, 249, 250], "core": [247, 248, 251]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def select_panel(rows):
    panels = {}
    for name, ids in COMPONENTS.items():
        panel = [row for row in rows if row["run_id"] in ids and (row["run_id"], row["question_id"]) != (248, 223)]
        if len(panel) != 98 or len({row["question_id"] for row in panel}) != 98:
            raise ValueError(f"{name}: expected 98 unique frozen answers")
        panels[name] = {row["question_id"]: row for row in panel}
    reference = {qid: (row["question"], row["gold_answer"]) for qid, row in panels["m0a"].items()}
    for name, panel in panels.items():
        if {qid: (row["question"], row["gold_answer"]) for qid, row in panel.items()} != reference:
            raise ValueError(f"{name}: question/gold drift")
        if any(row["error"] for row in panel.values()):
            raise ValueError(f"{name}: failed original item")
    return panels


def evaluate(args, environment):
    from src.goldset import eval as quality

    if os.getenv("JUDGE_RUBRIC_ADDENDUM", "").strip():
        raise ValueError("M1 rejudgment forbids an unregistered rubric addendum")
    dsn = environment["SCW_POSTGRES_DSN"]
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        baseline = conn.execute("SELECT * FROM rag_quality_eval_runs WHERE id=240").fetchone()
        rows = conn.execute(
            """SELECT i.run_id, i.question_id, i.question, i.gold_answer, i.gold_sources,
                      i.answer, i.contexts, i.sources, i.deterministic_metrics, i.error,
                      q.source AS corpus
               FROM rag_quality_eval_items i JOIN goldset_questions_v2 q ON q.id=i.question_id
               WHERE i.run_id=ANY(%s) ORDER BY i.question_id, i.run_id""",
            ([rid for ids in COMPONENTS.values() for rid in ids],),
        ).fetchall()
        if conn.execute(
            "SELECT id FROM rag_quality_eval_runs WHERE run_label=ANY(%s)", ([args.run_label + "_" + name for name in COMPONENTS],)
        ).fetchone():
            raise ValueError("Run labels must be new; never overwrite previous judgments")
    panels = select_panel(rows)
    manifest = {
        name: {str(qid): {"origin_run_id": row["run_id"], "input_sha256": digest(row)} for qid, row in panel.items()}
        for name, panel in panels.items()
    }
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (args.output_dir / "inputs.json").write_text(json.dumps(manifest, indent=2))
    if args.prepare_only:
        print(json.dumps({"panels": {name: len(panel) for name, panel in panels.items()}, "input_sha256": digest(manifest)}))
        return
    if list(args.output_dir.glob("item-*.json")):
        raise ValueError("Paid checkpoints already exist; reconcile them before any new run")
    config = quality.runtime_config_to_rag_config(quality.get_rag_config())
    prompt = quality.get_prompt_content(config.generation.system_prompt_name, render_today=False) or ""
    config_hash = quality.config_fingerprint(config, extra={"generator_system_prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()})
    if config_hash != baseline["config_fingerprint"]:
        raise ValueError("Configuration no longer matches M0a")
    scope = {**baseline["metadata"]["eval_scope"], "judge_evidence": quality.JUDGE_EVIDENCE_VERSION}
    if scope["judge_votes"] != 3 or baseline["judge_provider"] != "scaleway":
        raise ValueError("Expected the sovereign majority-three baseline")
    metadata = {
        "created_by": __name__,
        "database": "local_clone",
        "eval_scope": scope,
        "kind": "stored_answer_rejudgment",
        "input_sha256": digest(manifest),
        "source_fingerprint": source_fingerprint()["sha256"],
        "concurrency": 4,
    }
    run_ids = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for name in COMPONENTS:
            run_ids[name] = quality.create_eval_run(
                conn,
                goldset_name=baseline["goldset_name"],
                tags=["baseline_v1"],
                config=config,
                config_hash=config_hash,
                git_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                run_label=args.run_label + "_" + name,
                judge_provider="scaleway",
                judge_model=baseline["judge_model"],
                ragas_status="skipped",
                metadata={**metadata, "runtime": name, "origin_runs": COMPONENTS[name]},
            )
    (args.output_dir / "runs.json").write_text(json.dumps(run_ids))
    print(json.dumps({"run_ids": run_ids, **metadata}), flush=True)
    items = {name: [] for name in COMPONENTS}

    def judge(name, row):
        item = quality.EvalItem(
            **{
                key: row[key]
                for key in ("question_id", "question", "gold_answer", "gold_sources", "answer", "contexts", "sources", "deterministic_metrics")
            }
        )
        item.metadata = {"origin_run_id": row["run_id"], "input_sha256": digest(row)}
        item.ragas_metrics = {"status": "skipped", "reason": "disabled"}
        item.judge_result = quality.judge_answer_with_votes(
            votes=3,
            require_evidence=True,
            question=item.question,
            gold_answer=item.gold_answer,
            answer=item.answer,
            contexts=[str(c.get("content") or "") for c in item.contexts if str(c.get("content") or "").strip()],
            deterministic_metrics=item.deterministic_metrics,
            model=baseline["judge_model"],
            provider="scaleway",
            base_url=environment.get("SCALEWAY_BASE_URL", quality.DEFAULT_SCALEWAY_BASE_URL),
            api_key=environment["SCALEWAY_API_KEY"],
        )
        checkpoint = args.output_dir / f"item-{name}-{item.question_id}.json"
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(item), ensure_ascii=False))
        temporary.replace(checkpoint)
        with psycopg.connect(dsn) as conn:
            quality.insert_eval_item(conn, run_ids[name], item)
        return name, item

    fatal = ""
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(judge, name, panels[name][qid]) for qid in sorted(panels["m0a"]) for name in COMPONENTS]
            try:
                for future in as_completed(pending):
                    name, item = future.result()
                    items[name].append(item)
                    if item.judge_result.get("status") != "completed":
                        raise RuntimeError("Judge protocol failed: stopping pending calls; preserve all paid checkpoints")
                    print(
                        json.dumps(
                            {
                                "runtime": name,
                                "question_id": item.question_id,
                                "completed": len(items[name]),
                                "judge_status": item.judge_result.get("status"),
                            }
                        ),
                        flush=True,
                    )
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
    except BaseException as exc:
        fatal = type(exc).__name__
        raise
    finally:
        report = {"run_ids": run_ids, **metadata, "runtimes": {}}
        # Read checkpoints too: a DB interruption must not discard paid judgments.
        for name in COMPONENTS:
            items[name] = [quality.EvalItem(**json.loads(path.read_text())) for path in sorted(args.output_dir.glob(f"item-{name}-*.json"))]
            aggregate = quality.aggregate_items(items[name])
            aggregate["per_corpus"] = {
                corpus: quality.aggregate_items([item for item in items[name] if panels[name][item.question_id]["corpus"] == corpus])
                for corpus in sorted({row["corpus"] for row in panels[name].values()})
            }
            status, error = quality.derive_completion_status(items[name], judge_enabled=True, ragas_enabled=False, judge_votes=3)
            if fatal or len(items[name]) != 98:
                status, error = "failed", fatal or "incomplete_panel"
            report["runtimes"][name] = {"status": status, "aggregate": aggregate}
            with psycopg.connect(dsn) as conn:
                quality.complete_eval_run(conn, run_id=run_ids[name], status=status, aggregate=aggregate, error=error)
        (args.output_dir / "summary.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({"status": {name: row["status"] for name, row in report["runtimes"].items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-env", type=Path, required=True)
    parser.add_argument("--providers-env", type=Path, required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    evaluate(args, load_environment(args.local_env, args.providers_env))


if __name__ == "__main__":
    main()
