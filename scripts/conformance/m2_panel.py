"""Export the fingerprinted M1 questions for the live HTTP collector."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg
from assistant_rh_api.db.dsn import resolve_dsn, validate_libpq_environment
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from scripts.conformance.m0b_values import ROOT, canonical, digest
from scripts.conformance.m2_goldset_http import load_panel, open_private_capture
from src.goldset import eval as quality

M1_EVIDENCE = ROOT / "docs/evals/evidence/m1_api_parity_followup_20260930.json"


def readonly_staging_dsn(env_file):
    validate_libpq_environment(os.environ)
    environment = dotenv_values(env_file)
    staging = conninfo_to_dict(resolve_dsn(dsn=environment["SCW_POSTGRES_DSN_STAGING"]))
    production_dsn = environment.get("SCW_POSTGRES_DSN_PROD")
    if production_dsn:
        production = conninfo_to_dict(resolve_dsn(dsn=production_dsn))

        def targets(parameters):
            hosts = {host.strip().lower().rstrip(".") for key in ("host", "hostaddr") for host in parameters.get(key, "").split(",") if host}
            ports = {int(port or "5432") for port in parameters.get("port", "").split(",")}
            return {(host, port, parameters["dbname"]) for host in hosts for port in ports}

        if targets(staging) & targets(production):
            raise ValueError("Staging DSN matches production")
    options = staging.get("options", "")
    return make_conninfo(
        "",
        **(staging | {"options": f"{options} -c default_transaction_read_only=on -c statement_timeout=15000", "connect_timeout": 5}),
    )


def export_panel(dsn, output):
    evidence = json.loads(M1_EVIDENCE.read_text(encoding="utf-8"))
    reference = evidence["integrity"]["before"]
    questions = quality.load_goldset_questions(dsn, goldset_name="baseline_v1", tags=["baseline_v1"], any_goldset=True)
    panel_hash = hashlib.sha256(canonical(questions).encode()).hexdigest()
    if len(questions) != reference["question_count"] or panel_hash != reference["questions_fingerprint"]:
        raise ValueError("The database panel differs from the frozen M1 panel")

    maps = quality.load_gold_id_maps(dsn)
    rows = []
    for question in questions:
        scope = quality.resolve_question_scope(question, "per-question")
        rows.append(
            {
                "id": question.id,
                "model": f"assistant-rh-{scope.selected_ministry}",
                "question": question.question,
                "expected_doc_refs": sorted(set(quality.merge_gold_doc_ids(question.gold_doc_ids, question.gold_sources, maps))),
            }
        )
    with open_private_capture(output) as capture:
        for row in rows:
            capture.write(json.dumps(row, ensure_ascii=False) + "\n")
    load_panel(output)
    return {"questions": len(rows), "m1_questions_sha256": panel_hash, "http_panel_sha256": digest(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True, help="Reads SCW_POSTGRES_DSN_STAGING")
    parser.add_argument("--output", type=Path, required=True, help="New private JSONL path outside Git")
    args = parser.parse_args()
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        print(json.dumps(export_panel(readonly_staging_dsn(args.env_file), args.output)))
    except psycopg.Error as exc:
        raise SystemExit(f"Staging PostgreSQL export failed ({type(exc).__name__})") from None


if __name__ == "__main__":
    main()
