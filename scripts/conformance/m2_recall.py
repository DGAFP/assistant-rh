"""Measure M1 document recall from an unbounded, privately frozen capture."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

import psycopg
from assistant_rh_rag_pipeline.models import ContextItem, RetrievedChunk, _chunk_log_dict, context_item_document_id

from scripts.conformance.m0b_values import canonical, digest
from scripts.conformance.m2_goldset_http import load_panel
from scripts.conformance.m2_panel import M1_EVIDENCE, readonly_staging_dsn
from src.goldset import eval as quality


def evaluation_result(case):
    # Each attempt begins with retrieval. M1 uses the last attempt's diagnostics
    # and the actual final context, including after an unsuccessful selector retry.
    metadata = {}
    for stage in case["expected"]["stages"]:
        name, value = stage["stage"], stage["output"]
        if name == "retriever":
            metadata = {
                "chunks_raw": [_chunk_log_dict(RetrievedChunk(**chunk)) for chunk in value],
                "retrieved_chunks": [{"chunk_id": chunk["chunk_id"], "section_id": chunk["section_id"]} for chunk in value],
            }
        elif name == "section-aggregator":
            metadata["chunks_after_rerank"] = value["diagnostics"]["chunks_after_rerank"]
        elif name == "context-builder":
            metadata["context_items_ref"] = [{"doc_id": context_item_document_id(ContextItem(**item))} for item in value["items"]]
    items = [ContextItem(**item) for item in case["expected"]["result"]["context_items"]]
    return SimpleNamespace(metadata=metadata, context_items=items)


def summarize(rows):
    recalls = [row["doc_recall"] for row in rows if row["doc_recall"] is not None]
    return {
        "questions": len(rows),
        "recall_scored": len(recalls),
        "questions_with_hit": sum(int(row["hit_rate"]) for row in rows),
        "doc_recall_avg": mean(recalls),
    }


def evaluate(archive, archive_sha256, panel_file, panel_sha256, dsn):
    if digest(archive) != archive_sha256 or digest(panel_file) != panel_sha256:
        raise ValueError("Capture or M1 panel differs from its registered fingerprint")
    panel = {row["id"]: row for row in load_panel(panel_file)}
    aliases = quality.load_gold_id_maps(dsn)["aliases"]
    if not aliases:
        raise ValueError("No M1 corpus identifier aliases")
    aliases_sha256 = hashlib.sha256(canonical({key: sorted(values) for key, values in aliases.items()}).encode()).hexdigest()
    groups = defaultdict(list)
    seen = set()
    with archive.open(encoding="utf-8") as stream:
        for line in stream:
            case = json.loads(line)
            case_id = int(case["fixture"]["id"].removeprefix("q"))
            row = panel[case_id]
            if (
                case["schema"] != "m2-frozen-api-v1"
                or case_id in seen
                or case["fixture"]["query"] != row["question"]
                or case["api"]["model"] != row["model"]
            ):
                raise ValueError(f"Capture does not match the certified panel at question {case_id}")
            seen.add(case_id)
            result = evaluation_result(case)
            retrieved = quality.retrieved_doc_ids(result, quality.context_payload(result))
            metrics = quality.deterministic_metrics(row["expected_doc_refs"], retrieved, aliases=aliases)
            groups[row["model"]].append(metrics)
    if seen != panel.keys():
        raise ValueError("Capture does not cover the certified M1 panel")
    aggregate = summarize([row for rows in groups.values() for row in rows])
    reference = json.loads(M1_EVIDENCE.read_text(encoding="utf-8"))["assessment"]
    max_drop = reference["comparisons"]["core_live_vs_legacy_live"]["metrics"]["doc_recall_avg"]["max_drop"]
    comparisons = {}
    for label in ("legacy_live", "core_live", "m0a_rejudged"):
        baseline = reference["runs"][label]["aggregate"]["doc_recall_avg"]
        delta = aggregate["doc_recall_avg"] - baseline
        comparisons[label] = {"baseline": baseline, "delta": delta, "max_drop": max_drop, "within_threshold": delta >= -max_drop}
    return {
        "archive_sha256": archive_sha256,
        "panel_sha256": panel_sha256,
        "aliases_sha256": aliases_sha256,
        **aggregate,
        "by_model": {model: summarize(rows) for model, rows in sorted(groups.items())},
        "threshold_comparisons": comparisons,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--panel-sha256", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = evaluate(args.archive, args.archive_sha256, args.panel, args.panel_sha256, readonly_staging_dsn(args.env_file))
    except psycopg.Error as exc:
        raise SystemExit(f"Staging PostgreSQL alias lookup failed ({type(exc).__name__})") from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
