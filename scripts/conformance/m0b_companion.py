"""Record once from staging, then replay the complete production composition offline.

Run with ``python -m scripts.conformance.m0b_companion replay DIRECTORY``.
Only ``record`` reads staging and calls inference providers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from scripts.conformance.m0b_replay import replay_case
from scripts.conformance.m0b_values import BASELINE, PANEL, ROOT, assert_equal, digest, dump, require


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    panel = [json.loads(line) for line in PANEL.read_text().splitlines()]
    require(manifest["schema"] == "m0b-full-pipeline-companion-v1", "Unsupported companion schema")
    require(manifest["complete_panel"] is True and manifest["query_count"] == len(panel) == 7, "Exactly seven complete scenarios required")
    require(manifest["panel_sha256"] == digest(PANEL), "Scenario panel changed")
    require(manifest["original_manifest_sha256"] == digest(BASELINE / "manifest.json"), "Original M0b reference changed")
    require(set(manifest["files"]) == {row["id"] + ".json" for row in panel}, "Exactly seven fixtures required")
    cases = []
    for fixture in panel:
        name = fixture["id"] + ".json"
        path = directory / name
        require(not path.is_symlink() and path.resolve().parent == directory.resolve(), "Unsafe fixture path")
        require(digest(path) == manifest["files"][name], "Fixture hash mismatch")
        case = json.loads(path.read_text())
        assert_equal(case["fixture"], fixture, "original scenario")
        query = case["expected"]["stages"][0]["output"]
        observed = {
            "intent": query["intent"],
            "should_proceed": query["is_in_scope"],
            "needs_legal_search": query["needs_legal_search"],
            "selected_ministry": case["effective_ministry"],
            "generator_used_fallback": bool(case["observed"]["generator_used_fallback"]),
        }
        for key, value in fixture["expected"].items():
            assert_equal(observed[key], value, name + " branch " + key)
        cases.append(case)
    return manifest, cases


async def replay(directory):
    manifest, cases = verify(directory)
    results = []
    for case in cases:
        results.append(await replay_case(case))
    return {
        "exact_comparison": True,
        "network": "denied",
        "scope": manifest["scope"],
        "cases": results,
        "candidate_source_hashes": {
            str(path.relative_to(ROOT)): digest(path) for path in sorted((ROOT / "apps/api/src/assistant_rh_api").rglob("*.py"))
        },
    }


async def check(directory):
    await replay(directory)
    results = []
    for mutation in ("unhashed-fixture", "changed-provider-request", "changed-raw-rank", "changed-expected-answer", "extra-port-call"):
        with tempfile.TemporaryDirectory(prefix="m0b-negative-") as temp:
            copied = Path(temp) / "evidence"
            copied.mkdir()
            manifest, _ = verify(directory)
            for name in ["manifest.json", *manifest["files"]]:
                shutil.copyfile(directory / name, copied / name)
            path = copied / "rag-acronym-contract.json"
            case = json.loads(path.read_text())
            if mutation in ("unhashed-fixture", "changed-expected-answer"):
                case["expected"]["result"]["answer"] += " UNSUPPORTED CLAIM"
            elif mutation == "changed-provider-request":
                call = next(row for row in case["inference"] if row["operation"] == "llm.complete")
                call["request"]["messages"][-1]["content"] += " CHANGED PROMPT"
            elif mutation == "changed-raw-rank":
                call = next(row for row in case["stores"] if row["operation"] == "search.hybrid_candidates")
                call["response"][0][0]["rank"] += 999
            else:
                case["stores"].append(case["stores"][0])
            dump(path, case)
            if mutation != "unhashed-fixture":
                manifest["files"][path.name] = digest(path)
                dump(copied / "manifest.json", manifest)
            try:
                await replay(copied)
            except AssertionError as exc:
                results.append({"mutation": mutation, "detected": True, "error_type": type(exc).__name__})
            else:
                raise AssertionError("Mutation was not detected: " + mutation)
    return {"negative_controls": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "replay", "check"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--limit", type=int, help="Development capture only; a partial panel cannot pass verification")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.mode == "record":
        if args.env_file is None:
            parser.error("record requires --env-file")
        from scripts.conformance.m0b_record import record

        result = asyncio.run(record(args.directory, args.env_file, args.limit))
    else:
        result = asyncio.run((replay if args.mode == "replay" else check)(args.directory))
    if args.report:
        dump(args.report, result)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in ("source_hashes", "candidate_source_hashes")}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
