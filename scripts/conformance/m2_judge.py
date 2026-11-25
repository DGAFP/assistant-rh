"""Judge the captured staging responses with the three-vote M1 rubric."""

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from scripts.conformance.m0b_private import private_directory
from scripts.conformance.m0b_values import canonical, digest
from scripts.conformance.m2_goldset_http import load_panel
from scripts.conformance.m2_panel import M1_EVIDENCE, readonly_staging_dsn
from src.goldset import eval as quality

JUDGE_URL = "https://api.scaleway.ai/v1"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_private(path, value):
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.link(stream.name, path)


def index_unique(rows, key):
    indexed = {}
    for row in rows:
        identifier = key(row)
        if identifier in indexed:
            raise ValueError("Duplicate case or turn in the input capture")
        indexed[identifier] = row
    return indexed


def evidence_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare(campaign, env_file, output):
    panel_path = campaign.parent / "m1-http-panel.jsonl"
    answers_path, contexts_path = campaign / "events.jsonl", campaign / "campaign-contexts.jsonl"
    summary = json.loads((campaign / "aggregate.json").read_text(encoding="utf-8"))
    if digest(panel_path) != summary["panel_sha256"] or summary["persisted_responses_matching_http"] != 98:
        raise ValueError("The verified staging panel or response inventory differs")
    panel = {row["id"]: row for row in load_panel(panel_path)}
    answers = index_unique((row for row in read_jsonl(answers_path) if row["kind"] == "case"), lambda row: row["transport"]["id"])
    contexts = index_unique(read_jsonl(contexts_path), lambda row: row["turn_id"])
    if (
        len(panel) != 98
        or panel.keys() != answers.keys()
        or len(contexts) != len(panel)
        or contexts.keys() != {row["private"]["turn_id"] for row in answers.values()}
    ):
        raise ValueError("Expected a complete panel and all captured responses and contexts")

    proof = json.loads(M1_EVIDENCE.read_text(encoding="utf-8"))
    dsn = readonly_staging_dsn(env_file)
    questions = quality.load_goldset_questions(dsn, goldset_name="baseline_v1", tags=["baseline_v1"], any_goldset=True)
    if hashlib.sha256(canonical(questions).encode()).hexdigest() != proof["integrity"]["before"]["questions_fingerprint"]:
        raise ValueError("The M1 database questions differ from their published fingerprint")
    gold_answers = {question.id: question.gold_answer for question in questions}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        references = conn.execute(
            """SELECT i.question_id, i.question, i.gold_answer, q.source AS corpus
               FROM public.rag_quality_eval_items i JOIN public.goldset_questions_v2 q ON q.id=i.question_id
               WHERE i.run_id=240 ORDER BY i.question_id"""
        ).fetchall()
    if len(references) != 98 or {row["question_id"] for row in references} != panel.keys():
        raise ValueError("Expected exactly the M0a #240 reference panel")

    cases = []
    for row in references:
        question_id = row["question_id"]
        response = answers[question_id]
        transport, private = response["transport"], response["private"]
        context = contexts[private["turn_id"]]
        excerpts = context["contexts"]
        context_status = "trace_projection_and_prompt_prefix_matched" if excerpts else "no_generation_context"
        if (
            context["context_status"] != context_status
            or not isinstance(excerpts, list)
            or not all(isinstance(excerpt, str) and excerpt.strip() for excerpt in excerpts)
            or transport["outcome"] != "completed"
            or context["status"] != "completed"
            or private["id"] != question_id
            or transport["model"] != panel[question_id]["model"]
            or context["model"] != transport["model"]
            or context["answer_sha256"] != evidence_digest(private["answer"])
            or context["sources_sha256"] != evidence_digest(private["sources"])
            or row["question"] != panel[question_id]["question"]
            or row["gold_answer"] != gold_answers[question_id]
        ):
            raise ValueError(f"Unverified input for case {question_id}")
        cases.append(row | {"answer": private["answer"], "contexts": excerpts})
    write_private(output / "judge-inputs.json", cases)
    reference = proof["assessment"]["runs"]["core_live"]["fingerprints"]["eval_scope"]
    manifest = {
        "cases": len(cases),
        "input_sha256": digest(output / "judge-inputs.json"),
        "panel_sha256": digest(panel_path),
        "http_events_sha256": digest(answers_path),
        "reconstructed_contexts_sha256": digest(contexts_path),
        "m0a_reference_sha256": hashlib.sha256(canonical(references).encode()).hexdigest(),
        "judge_provider": "scaleway",
        "judge_endpoint": JUDGE_URL,
        "judge_model": reference["judge_model"],
        "judge_votes": reference["judge_votes"],
        "judge_evidence": reference["judge_evidence"],
        "retrieval_diagnostics": "omitted: complete retrieval inventory unavailable for the captured staging responses",
    }
    write_private(output / "judge-manifest.json", manifest)
    return manifest


def judge(output, env_file):
    manifest_path = output / "judge-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_fingerprint = digest(manifest_path)
    path = output / "judge-inputs.json"
    if (
        digest(path) != manifest["input_sha256"]
        or manifest["judge_evidence"] != quality.JUDGE_EVIDENCE_VERSION
        or manifest["judge_votes"] != 3
        or manifest["judge_provider"] != "scaleway"
        or manifest["judge_endpoint"] != JUDGE_URL
    ):
        raise ValueError("Judge input or evidence protocol changed since preparation")
    cases = json.loads(path.read_text(encoding="utf-8"))
    if len(cases) != manifest["cases"] or len({row["question_id"] for row in cases}) != 98:
        raise ValueError("Expected all 98 distinct inputs")
    if os.getenv("JUDGE_RUBRIC_ADDENDUM", "").strip():
        raise ValueError("An extra judge rubric is incompatible with the M1 reference")
    key = dotenv_values(env_file).get("SCALEWAY_API_KEY")
    if not key:
        raise ValueError("Scaleway judge credential is unavailable")

    def evaluate(row):
        checkpoint = output / f"judge-{row['question_id']}.json"
        if checkpoint.is_file():
            result = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (
                result["run_fingerprint"] != run_fingerprint
                or result["question_id"] != row["question_id"]
                or result["corpus"] != row["corpus"]
                or len(result["judgment"]["votes"]) != manifest["judge_votes"]
            ):
                raise ValueError(f"Checkpoint does not match the M1 judge run for question {row['question_id']}")
            return result
        payload = quality.judge_answer_with_votes(
            votes=manifest["judge_votes"],
            require_evidence=True,
            question=row["question"],
            gold_answer=row["gold_answer"],
            answer=row["answer"],
            contexts=row["contexts"],
            deterministic_metrics=row.get("deterministic_metrics", {}),
            model=manifest["judge_model"],
            provider="scaleway",
            base_url=JUDGE_URL,
            api_key=key,
        )
        result = {"run_fingerprint": run_fingerprint, "question_id": row["question_id"], "corpus": row["corpus"], "judgment": payload}
        write_private(checkpoint, result)
        return result

    totals = Counter()
    corpus_totals = defaultdict(Counter)
    failed_votes = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(evaluate, row) for row in cases]
        for count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            verdict = result["judgment"]
            outcome = ("pass" if verdict["pass"] else "fail") if verdict["status"] == "completed" else "unjudged"
            totals[outcome] += 1
            corpus_totals[result["corpus"]][outcome] += 1
            failed_votes += sum(vote["status"] != "completed" for vote in verdict.get("votes", []))
            if count % 10 == 0 or count == len(cases):
                print(json.dumps({"processed": count, **totals}), flush=True)
    summary = {"manifest": manifest, "outcomes": totals, "by_corpus": corpus_totals, "failed_votes": failed_votes}
    write_private(output / "judge-summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "judge"))
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    output = private_directory(args.output)
    if args.mode == "prepare":
        if args.campaign is None:
            parser.error("prepare requires --campaign")
        result = prepare(args.campaign, args.env_file, output)
    else:
        result = judge(output, args.env_file)
    print(json.dumps(result, ensure_ascii=False))
    if args.mode == "judge" and result["outcomes"].get("unjudged"):
        raise SystemExit("One or more questions did not reach a judge quorum")


if __name__ == "__main__":
    main()
