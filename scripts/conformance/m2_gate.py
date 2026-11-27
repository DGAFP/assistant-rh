"""Prepare and compare a three-vote M1 judgment on the capture scored for recall."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from assistant_rh_api.core.models.conversations import RunSource
from assistant_rh_api.core.sources import SOURCES_MARKER, with_sources

from scripts.conformance.m0b_private import private_directory
from scripts.conformance.m0b_values import ROOT, digest
from scripts.conformance.m2_goldset_http import load_panel
from scripts.conformance.m2_judge import JUDGE_URL, index_unique, write_private
from scripts.conformance.m2_panel import M1_EVIDENCE, readonly_staging_dsn
from scripts.conformance.m2_recall import load_aliases, scored_cases, summarize
from src.goldset import eval as quality

FOLLOWUP = ROOT / "docs/evals/evidence/m2_d3_staging_followup_20261125.json"
GOLDSET = "baseline_v1"
REFERENCES = ("legacy_live", "core_live", "m0a_rejudged")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def capture_binding(proof):
    capture, recall, replay = (proof[key] for key in ("capture", "retrieval_recall", "exact_http_replay"))
    if capture["sha256"] != recall["archive_sha256"] or capture["sha256"] != replay["sha256"] or not replay["http_json_exact"]:
        raise ValueError("Published recall and exact HTTP replay must cover the same capture")
    return {
        "capture_campaign": capture["campaign"],
        "archive_sha256": capture["sha256"],
        "panel_sha256": proof["panel_sha256"],
        "aliases_sha256": recall["aliases_sha256"],
        "reference_input_sha256": proof["judge"]["manifest"]["input_sha256"],
    }


def prepare(archive, panel_file, reference_inputs, dsn, output):
    proof = read_json(FOLLOWUP)
    binding = capture_binding(proof)
    assessment = read_json(M1_EVIDENCE)["assessment"]
    if (
        digest(archive) != binding["archive_sha256"]
        or digest(panel_file) != binding["panel_sha256"]
        or digest(reference_inputs) != binding["reference_input_sha256"]
    ):
        raise ValueError("An input does not match the previously verified capture, panel or references")
    panel = {row["id"]: row for row in load_panel(panel_file)}
    references = index_unique(read_json(reference_inputs), lambda row: row["question_id"])
    scope = assessment["runs"]["core_live"]["fingerprints"]["eval_scope"]
    if len(panel) != 98 or panel.keys() != references.keys() or sorted(panel) != scope["question_ids"]:
        raise ValueError("Question IDs differ from the complete M1 panel")
    if (scope["judge_provider"], scope["judge_base_url"], scope["judge_votes"], scope["judge_evidence"]) != (
        "scaleway",
        JUDGE_URL,
        3,
        quality.JUDGE_EVIDENCE_VERSION,
    ):
        raise ValueError("Unexpected M1 evaluator settings")
    aliases, aliases_sha256 = load_aliases(dsn)
    if aliases_sha256 != binding["aliases_sha256"]:
        raise ValueError("Document aliases changed since the capture was scored")

    cases = []
    retrieval_by_corpus = defaultdict(list)
    judge_diagnostics = ("doc_recall", "doc_precision", "hit_rate", "missing_gold_sources")
    for row, case, contexts, metrics in scored_cases(archive, panel, aliases):
        reference = references[row["id"]]
        served = case["api"]["answer"]
        sources = tuple(RunSource(**source) for source in case["api"]["sources"])
        if reference["question"] != row["question"] or with_sources(case["expected"]["result"]["answer"], sources) != served:
            raise ValueError(f"Question or captured response mismatch at case {row['id']}")
        excerpts = [str(context.get("content") or "") for context in contexts if str(context.get("content") or "").strip()]
        cases.append(
            {
                "question_id": row["id"],
                "question": row["question"],
                "gold_answer": reference["gold_answer"],
                "corpus": reference["corpus"],
                "answer": served.partition(SOURCES_MARKER)[0],
                "contexts": excerpts,
                "deterministic_metrics": {key: metrics[key] for key in judge_diagnostics},
            }
        )
        retrieval_by_corpus[reference["corpus"]].append(metrics)
    cases.sort(key=lambda row: row["question_id"])
    retrieval = summarize([row for rows in retrieval_by_corpus.values() for row in rows])
    if any(retrieval[key] != proof["retrieval_recall"][key] for key in retrieval):
        raise ValueError("Recall recomputed for judge inputs does not match the verified capture")
    inputs = output / "judge-inputs.json"
    write_private(inputs, cases)
    manifest = {
        "run_label": "m1_gate_same_capture_20261125",
        **binding,
        "input_sha256": digest(inputs),
        "cases": len(cases),
        "judge_provider": scope["judge_provider"],
        "judge_endpoint": scope["judge_base_url"],
        "judge_model": scope["judge_model"],
        "judge_votes": scope["judge_votes"],
        "judge_evidence": scope["judge_evidence"],
        "answer_projection": "served answer body before the API-appended sources block",
        "retrieval_diagnostics": "from the same frozen case as the answer and generation context",
        "eval_scope": scope,
        "retrieval": retrieval | {"by_corpus": {name: summarize(rows) for name, rows in sorted(retrieval_by_corpus.items())}},
    }
    write_private(output / "judge-manifest.json", manifest)
    return manifest


def report(output):
    proof = read_json(FOLLOWUP)
    binding = capture_binding(proof)
    assessment = read_json(M1_EVIDENCE)["assessment"]
    manifest = read_json(output / "judge-manifest.json")
    judgment = read_json(output / "judge-summary.json")
    if judgment["manifest"] != manifest or digest(output / "judge-inputs.json") != manifest["input_sha256"]:
        raise ValueError("Judge summary does not match the registered inputs")
    counts = judgment["outcomes"]
    judged = counts.get("pass", 0) + counts.get("fail", 0)
    total = manifest["cases"]
    retrieval = manifest["retrieval"]
    scope = assessment["runs"]["core_live"]["fingerprints"]["eval_scope"]
    if (
        any(manifest.get(key) != value for key, value in binding.items())
        or manifest["eval_scope"] != scope
        or total != scope["question_count"]
        or total != proof["exact_http_replay"]["cases"]
        or any(retrieval[key] != proof["retrieval_recall"][key] for key in ("questions", "recall_scored", "questions_with_hit", "doc_recall_avg"))
    ):
        raise ValueError("Gate does not match the registered M1 capture and scope")
    complete = judged == sum(counts.values()) == total == retrieval["recall_scored"] and not counts.get("unjudged")
    aggregate = {
        "judge_pass_rate": counts.get("pass", 0) / judged if judged else None,
        "doc_recall_avg": retrieval["doc_recall_avg"],
    }
    gate_thresholds = assessment["comparisons"]["core_live_vs_legacy_live"]["metrics"]
    comparisons = {}
    for name in REFERENCES:
        run = assessment["runs"][name]
        baseline = run | {"goldset_name": GOLDSET, "tag_filter": [GOLDSET], "metadata": run["fingerprints"]}
        comparisons[name] = quality.compare_with_baseline(
            candidate_aggregate=aggregate,
            baseline_run=baseline,
            goldset_name=GOLDSET,
            tags=[GOLDSET],
            eval_scope=manifest["eval_scope"],
            max_judge_pass_rate_drop=gate_thresholds["judge_pass_rate"]["max_drop"],
            max_doc_recall_drop=gate_thresholds["doc_recall_avg"]["max_drop"],
        )
    return {
        "schema": "assistant-rh-m1-same-capture-gate-v1",
        "manifest": manifest,
        "deployment": proof["deployment"],
        "judgment": {key: judgment[key] for key in ("outcomes", "by_corpus", "failed_votes")},
        "aggregate": aggregate,
        "complete": complete,
        "comparisons": comparisons,
        "gate": "pass" if complete and all(result["passed"] for result in comparisons.values()) else "fail",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "report"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--panel", type=Path)
    parser.add_argument("--reference-inputs", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    output = private_directory(args.output)
    if args.mode == "prepare":
        if None in (args.archive, args.panel, args.reference_inputs, args.env_file):
            parser.error("prepare requires --archive, --panel, --reference-inputs and --env-file")
        result = prepare(args.archive, args.panel, args.reference_inputs, readonly_staging_dsn(args.env_file), output)
    else:
        result = report(output)
    print(json.dumps(result, ensure_ascii=False))
    if args.mode == "report" and result["gate"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
