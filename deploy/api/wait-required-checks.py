#!/usr/bin/env python3
"""Fail closed until required push workflows succeed on this exact delivery revision."""

import json
import os
import re
import subprocess
import time

REQUIRED = ["ci-tests.yml", "codeql.yml"]
REPOSITORY = "DGAFP/assistant-rh"
BRANCH = "staging"


def evaluate(runs, sha):
    pending = []
    for path in REQUIRED:
        matches = [
            r
            for r in runs
            if r.get("head_sha") == sha
            and r.get("head_branch") == BRANCH
            and r.get("event") == "push"
            and r.get("path") == ".github/workflows/" + path
        ]
        if not matches:
            pending.append(path)
            continue
        latest = max(matches, key=lambda r: (r["id"], r.get("run_attempt", 1)))
        if latest["status"] != "completed":
            pending.append(path)
        elif latest.get("conclusion") != "success":
            raise RuntimeError(path + " did not succeed on the requested SHA")
    return pending


def main():
    if os.environ["GITHUB_REPOSITORY"] != REPOSITORY or os.environ["GITHUB_REF_NAME"] != BRANCH:
        raise RuntimeError("Unexpected repository or delivery branch")
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("Invalid commit SHA")
    deadline = time.monotonic() + 3600
    while True:
        result = subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/actions/runs?head_sha={sha}&event=push&per_page=100"], capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise RuntimeError("GitHub checks could not be read; refusing publication")
        payload = json.loads(result.stdout)
        if payload["total_count"] > 100:
            raise RuntimeError("Too many workflow runs to validate unambiguously")
        pending = evaluate(payload["workflow_runs"], sha)
        if not pending:
            print("Required push workflows succeeded on exact delivery SHA:", sha)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Required checks still missing or incomplete")
        print("Waiting for:", ", ".join(pending), flush=True)
        time.sleep(20)


if __name__ == "__main__":
    main()
