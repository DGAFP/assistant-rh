"""Judge the captured staging responses with the three-vote M1 rubric."""

import argparse
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from scripts.conformance.m0b_private import private_directory
from scripts.conformance.m0b_values import digest
from scripts.conformance.m2_goldset_http import open_private_capture
from scripts.conformance.m2_panel import M1_EVIDENCE, readonly_staging_dsn
from src.goldset import eval as quality

JUDGE_URL = "https://api.scaleway.ai/v1"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_private(path, value):
    with open_private_capture(path) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def prepare(campaign, env_file, output):
    panel_path = campaign.parent / "m1-http-panel.jsonl"
    summary = json.loads((campaign / "aggregate.json").read_text(encoding="utf-8"))
    if digest(panel_path) != summary["panel_sha256"] or summary["persisted_responses_matching_http"] != 98:
        raise ValueError("The verified staging panel or response inventory differs")
    panel = {row["id"]: row for row in read_jsonl(panel_path)}
    answers = {row["transport"]["id"]: row for row in read_jsonl(campaign / "events.jsonl") if row["kind"] == "case"}
    contexts = {row["turn_id"]: row for row in read_jsonl(campaign / "campaign-contexts.jsonl")}
    if len(panel) != 98 or panel.keys() != answers.keys() or len(contexts) != 98:
        raise ValueError("Expected a complete panel and all captured responses and contexts")

    with psycopg.connect(readonly_staging_dsn(env_file), row_factory=dict_row) as conn:
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
        private = response["private"]
        context = contexts[private["turn_id"]]
        valid_context = context["context_status"] in ("trace_projection_and_prompt_prefix_matched", "no_generation_context")
        if not valid_context or response["transport"]["outcome"] != "completed" or row["question"] != panel[question_id]["question"]:
            raise ValueError(f"Unverified input for case {question_id}")
        cases.append(row | {"answer": private["answer"], "contexts": context["contexts"]})
    write_private(output / "judge-inputs.json", cases)
    proof = json.loads(M1_EVIDENCE.read_text(encoding="utf-8"))
    reference = proof["assessment"]["runs"]["core_live"]["fingerprints"]["eval_scope"]
    manifest = {
        "cases": len(cases),
        "input_sha256": digest(output / "judge-inputs.json"),
        "panel_sha256": digest(panel_path),
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
    manifest = json.loads((output / "judge-manifest.json").read_text(encoding="utf-8"))
    path = output / "judge-inputs.json"
    if digest(path) != manifest["input_sha256"] or manifest["judge_evidence"] != quality.JUDGE_EVIDENCE_VERSION:
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
            return json.loads(checkpoint.read_text(encoding="utf-8"))
        payload = quality.judge_answer_with_votes(
            votes=manifest["judge_votes"],
            require_evidence=True,
            question=row["question"],
            gold_answer=row["gold_answer"],
            answer=row["answer"],
            contexts=row["contexts"],
            deterministic_metrics={},
            model=manifest["judge_model"],
            provider="scaleway",
            base_url=JUDGE_URL,
            api_key=key,
        )
        result = {"question_id": row["question_id"], "corpus": row["corpus"], "judgment": payload}
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


if __name__ == "__main__":
    main()
