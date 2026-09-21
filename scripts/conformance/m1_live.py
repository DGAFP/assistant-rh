"""Paired live M1 evaluation on a local RAG clone; the existing judge and metrics are reused.

Run with ``python -m scripts.conformance.m1_live --help``. No remote database writes.
Journal the label and settings before launching, then record the resulting run IDs.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[2]


def load_environment(local_env, providers_env):
    local = {key: value for key, value in dotenv_values(local_env).items() if value is not None}
    target = conninfo_to_dict(local["SCW_POSTGRES_DSN"])
    if target.get("host") not in ("127.0.0.1", "localhost", "::1") or not target.get("dbname", "").endswith("_rag_local"):
        raise ValueError("M1 requires an explicit local RAG clone")
    providers = dotenv_values(providers_env)
    for key in ("ALBERT_API_KEY", "ALBERT_BASE_URL", "ALBERT_EMBED_MODEL", "ALBERT_RERANK_MODEL", "SCALEWAY_API_KEY", "SCALEWAY_BASE_URL"):
        if providers.get(key):
            local[key] = providers[key]
    local.update(APP_ENV="local", PYTHON_DOTENV_DISABLED="1", RAG_TRACING_ENABLED="false")
    os.environ.update(local)
    return local


def source_fingerprint():
    folders = ("apps/api/src", "packages/rag-pipeline/src", "src/goldset", "scripts/conformance")
    paths = [path for folder in folders for path in (ROOT / folder).rglob("*.py")]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
    return {"sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), "files": hashes}


async def evaluate(args, environment):
    import httpx
    import psycopg
    from assistant_rh_api.bootstrap import create_chat_service
    from assistant_rh_api.core.rag_configuration import RAGConfigurationService
    from assistant_rh_api.db.dsn import DatabaseSettings
    from assistant_rh_api.db.pool import Database
    from assistant_rh_api.db.settings_stores import ConfigStore
    from assistant_rh_api.gateways.auth import SystemClock
    from psycopg.rows import dict_row
    from sqlalchemy import create_engine

    from scripts.conformance.m0b_values import canonical, plain
    from scripts.conformance.m1_runtime import CoreEvaluator, LegacyEvaluator
    from src.goldset import eval as quality

    dsn = environment["SCW_POSTGRES_DSN"]
    runtime_config = quality.get_rag_config()
    config = quality.runtime_config_to_rag_config(runtime_config)
    prompt = quality.get_prompt_content(config.generation.system_prompt_name, render_today=False) or ""
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    config_hash = quality.config_fingerprint(config, extra={"generator_system_prompt_sha": prompt_hash})
    questions = quality.load_goldset_questions(dsn, goldset_name="baseline_v1", tags=["baseline_v1"], any_goldset=True)
    if args.question_ids:
        questions = [question for question in questions if question.id in args.question_ids]
        if {question.id for question in questions} != set(args.question_ids):
            raise ValueError("Some requested questions are outside the frozen panel")
    elif len(questions) != 98:
        raise ValueError("Full M1 requires the 98-question M0a panel")
    raw_question_hash = hashlib.sha256(canonical(questions).encode()).hexdigest()
    maps = quality.load_gold_id_maps(dsn)
    for question in questions:
        question.gold_doc_ids = quality.merge_gold_doc_ids(question.gold_doc_ids, question.gold_sources, maps)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        baseline = conn.execute("SELECT * FROM rag_quality_eval_runs WHERE id=240").fetchone()
        if baseline is None or baseline["config_fingerprint"] != config_hash:
            raise ValueError("Configuration does not match M0a #240")
        labels = [args.run_label + "_legacy", args.run_label + "_core"]
        existing = conn.execute("SELECT id FROM rag_quality_eval_runs WHERE run_label=ANY(%s)", (labels,)).fetchall()
        if existing:
            raise ValueError("Run labels must be unique; completed or partial runs are never overwritten")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources = source_fingerprint()
    (args.output_dir / "source-fingerprint.json").write_text(json.dumps(sources, indent=2))
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    scope_args = quality.build_parser().parse_args(
        [
            "--goldset-name",
            baseline["goldset_name"],
            "--any-goldset",
            "--tag",
            "baseline_v1",
            "--skip-ragas",
            "--judge-votes",
            "3",
        ]
    )
    scope_args.judge_model = baseline["judge_model"]
    eval_scope = quality.build_eval_scope(scope_args, questions)
    metadata = {
        "created_by": "scripts/conformance/m1_live.py",
        "source_fingerprint": sources["sha256"],
        "questions_fingerprint": raw_question_hash,
        "generator_system_prompt_sha": prompt_hash,
        "eval_scope": eval_scope,
        "paired_label": args.run_label,
        "database": "local_clone",
        "concurrency": args.concurrency,
        "m0a_run_id": 240,
    }
    run_ids = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for runtime in ("legacy", "core"):
            run_ids[runtime] = quality.create_eval_run(
                conn,
                goldset_name=baseline["goldset_name"],
                tags=["baseline_v1"],
                config=config,
                config_hash=config_hash,
                git_sha=git_sha,
                run_label=args.run_label + "_" + runtime,
                judge_provider="scaleway",
                judge_model=baseline["judge_model"],
                ragas_status="skipped",
                metadata={**metadata, "runtime": runtime},
            )
    print(json.dumps({"run_ids": run_ids, "questions": len(questions), "source_fingerprint": sources["sha256"]}), flush=True)
    items = {runtime: [] for runtime in run_ids}
    database = Database(DatabaseSettings(dsn=dsn, max_size=8))
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://"))
    fatal = ""
    try:
        await database.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            service = create_chat_service(database, RAGConfigurationService(ConfigStore(database)), client, environment)
            semaphore = asyncio.Semaphore(args.concurrency)
            loop = asyncio.get_running_loop()

            async def run_pair(question):
                async with semaphore:
                    evaluators = {
                        "legacy": LegacyEvaluator(config, runtime_config, dsn, engine, args.run_label),
                        "core": CoreEvaluator(service, loop, SystemClock(), args.run_label),
                    }

                    async def run_one(runtime):
                        item = await asyncio.to_thread(
                            quality.run_question_with_retry,
                            pipe=evaluators[runtime],
                            question=question,
                            identifier_aliases=maps.get("aliases", {}),
                            run_ragas=False,
                            run_judge=True,
                            judge_model=baseline["judge_model"],
                            judge_base_url=environment.get("SCALEWAY_BASE_URL", quality.DEFAULT_SCALEWAY_BASE_URL),
                            judge_api_key=environment["SCALEWAY_API_KEY"],
                            judge_provider="scaleway",
                            judge_votes=3,
                            ragas_model="",
                            scaleway_base_url="",
                            scaleway_api_key="",
                            retrieval_scope=quality.resolve_question_scope(question, "per-question"),
                        )
                        with psycopg.connect(dsn) as conn:
                            quality.insert_eval_item(conn, run_ids[runtime], item)
                        items[runtime].append(item)
                        print(
                            json.dumps(
                                {
                                    "runtime": runtime,
                                    "question_id": question.id,
                                    "completed": len(items[runtime]),
                                    "error": bool(item.error),
                                    "judge_status": item.judge_result.get("status"),
                                }
                            ),
                            flush=True,
                        )

                    await asyncio.gather(*(run_one(runtime) for runtime in run_ids))

            outcomes = await asyncio.gather(*(run_pair(question) for question in questions), return_exceptions=True)
            failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
            if failures:
                raise failures[0]
    except Exception as exc:
        fatal = type(exc).__name__
        raise
    finally:
        await database.close()
        engine.dispose()
        report = {"run_ids": run_ids, "config_fingerprint": config_hash, **metadata, "runtimes": {}}
        for runtime in run_ids:
            ordered = sorted(items[runtime], key=lambda item: item.question_id)
            aggregate = quality.aggregate_items(ordered)
            status, error = quality.derive_completion_status(ordered, judge_enabled=True, ragas_enabled=False, judge_votes=3)
            if fatal or len(ordered) != len(questions):
                status, error = "failed", fatal or "incomplete_panel"
            aggregate["per_corpus"] = {
                source: quality.aggregate_items([item for item in ordered if item.question_id in {q.id for q in questions if q.source == source}])
                for source in sorted({question.source for question in questions})
            }
            report["runtimes"][runtime] = {"run_id": run_ids[runtime], "status": status, "aggregate": aggregate}
            with psycopg.connect(dsn) as conn:
                quality.complete_eval_run(conn, run_id=run_ids[runtime], status=status, aggregate=aggregate, error=error)
            quality.write_artifacts(args.output_dir, args.run_label + "_" + runtime, ordered, report["runtimes"][runtime])
        (args.output_dir / "paired-summary.json").write_text(json.dumps(plain(report), indent=2))
        print(
            json.dumps(
                {
                    "status": {name: row["status"] for name, row in report["runtimes"].items()},
                    "report": str(args.output_dir / "paired-summary.json"),
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-env", type=Path, required=True)
    parser.add_argument("--providers-env", type=Path, required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--question-ids", type=int, nargs="+")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    environment = load_environment(args.local_env, args.providers_env)
    asyncio.run(evaluate(args, environment))


if __name__ == "__main__":
    main()
