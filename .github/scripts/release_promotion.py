#!/usr/bin/env python3
"""Orchestrate a single Release Please PR from staging to main.

Release Please only supports a single target branch. This helper builds a
candidate branch that contains both the current main release metadata and the
latest staging revision, lets Release Please prepare its normal release commit
on top of that candidate, and then retargets the generated PR to main.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

CANDIDATE_BRANCH = "release-candidate"
RELEASE_BRANCH_PREFIX = f"release-please--branches--{CANDIDATE_BRANCH}"
RELEASE_PR_MARKER = "<!-- staging-main-release-promotion -->"
RELEASE_PR_INTRO = f"""{RELEASE_PR_MARKER}
> Cette PR est l'unique promotion **staging → main**. Elle contient la
> révision staging validée ainsi que les fichiers de version et le changelog
> préparés par Release Please. Son merge publiera le tag et la GitHub Release.

"""
REQUIRED_STAGING_WORKFLOWS = frozenset(
    {
        "CI Tests",
        "CodeQL",
        "Conformance",
        "Streamlit Deploy Staging",
    }
)
SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
SELF_WORKFLOW = "Release Please"
# Sentinel returned by GitHubClient.api when a direct merge is rejected
# (conflict or branch protection) and the caller opted into handling it.
MERGE_BLOCKED = object()


class PromotionError(RuntimeError):
    """Raised when the promotion cannot be proven safe."""


class GitHubClient:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def api(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
        allow_merge_blocked: bool = False,
    ) -> Any:
        command = ["gh", "api", "--method", method, endpoint]
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = json.dumps(payload)

        result = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            if allow_not_found and "HTTP 404" in result.stderr:
                return None
            if allow_merge_blocked and any(code in result.stderr for code in ("HTTP 403", "HTTP 405", "HTTP 409")):
                return MERGE_BLOCKED
            raise PromotionError(f"GitHub API {method} {endpoint} failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def graphql(self, query: str, variables: dict[str, str]) -> Any:
        command = ["gh", "api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            command.extend(["-f", f"{name}={value}"])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise PromotionError(f"GitHub GraphQL request failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def ref_sha(self, branch: str) -> str:
        ref = self.api("GET", f"repos/{self.repo}/git/ref/heads/{quote(branch, safe='')}")
        return str(ref["object"]["sha"])

    def set_draft(self, pull_request: dict[str, Any], draft: bool) -> None:
        if bool(pull_request.get("draft")) == draft:
            return
        mutation = (
            "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
            if draft
            else "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
        )
        self.graphql(mutation, {"id": str(pull_request["node_id"])})


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def append_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(markdown.rstrip() + "\n")


def release_pull_requests(pulls: Iterable[dict[str, Any]], repo: str) -> list[dict[str, Any]]:
    matches = []
    for pull in pulls:
        head = pull.get("head") or {}
        head_repo = head.get("repo") or {}
        if head_repo.get("full_name", "").lower() != repo.lower():
            continue
        if str(head.get("ref", "")).startswith(RELEASE_BRANCH_PREFIX):
            matches.append(pull)
    return matches


def get_open_release_pr(client: GitHubClient) -> dict[str, Any] | None:
    pulls = client.api("GET", f"repos/{client.repo}/pulls?state=open&per_page=100")
    matches = release_pull_requests(pulls, client.repo)
    if len(matches) > 1:
        numbers = ", ".join(str(pull["number"]) for pull in matches)
        raise PromotionError(f"Multiple open release promotion PRs found: {numbers}")
    return matches[0] if matches else None


def compare_contains(client: GitHubClient, base: str, head: str, expected_base_sha: str) -> None:
    comparison = client.api(
        "GET",
        f"repos/{client.repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
    )
    merge_base_sha = str((comparison.get("merge_base_commit") or {}).get("sha", ""))
    if comparison.get("status") not in {"ahead", "identical"} or merge_base_sha != expected_base_sha:
        raise PromotionError(f"{head} does not contain the expected {base} revision {expected_base_sha}")


def prepare(client: GitHubClient) -> None:
    main_sha = client.ref_sha("main")
    staging_sha = client.ref_sha("staging")
    candidate_ref_endpoint = f"repos/{client.repo}/git/ref/heads/{CANDIDATE_BRANCH}"
    candidate_update_endpoint = f"repos/{client.repo}/git/refs/heads/{CANDIDATE_BRANCH}"
    candidate = client.api("GET", candidate_ref_endpoint, allow_not_found=True)
    if candidate is None:
        client.api(
            "POST",
            f"repos/{client.repo}/git/refs",
            {"ref": f"refs/heads/{CANDIDATE_BRANCH}", "sha": main_sha},
        )
    else:
        client.api("PATCH", candidate_update_endpoint, {"sha": main_sha, "force": True})

    merge = client.api(
        "POST",
        f"repos/{client.repo}/merges",
        {
            "base": CANDIDATE_BRANCH,
            "head": staging_sha,
            "commit_message": f"chore(release): merge staging {staging_sha[:12]} into release candidate",
        },
    )
    candidate_sha = str(merge["sha"]) if merge else main_sha

    existing_pr = get_open_release_pr(client)
    if existing_pr:
        base_ref = str((existing_pr.get("base") or {}).get("ref", ""))
        if base_ref not in {"main", CANDIDATE_BRANCH}:
            raise PromotionError(f"Release PR #{existing_pr['number']} has unexpected base {base_ref!r}")
        client.set_draft(existing_pr, True)
        if base_ref != CANDIDATE_BRANCH:
            client.api(
                "PATCH",
                f"repos/{client.repo}/pulls/{existing_pr['number']}",
                {"base": CANDIDATE_BRANCH},
            )

    write_output("main_sha", main_sha)
    write_output("staging_sha", staging_sha)
    write_output("candidate_sha", candidate_sha)
    write_output("existing_pr_number", str(existing_pr["number"]) if existing_pr else "")


def finalize(client: GitHubClient, staging_sha: str, main_sha: str) -> None:
    pull = get_open_release_pr(client)
    if pull is None:
        write_output("has_release_pr", "false")
        append_summary("## Release promotion\n\nNo releasable commit was found; no promotion PR was created.")
        return

    labels = {str(label.get("name", "")) for label in pull.get("labels", [])}
    if "autorelease: pending" not in labels:
        raise PromotionError(f"Release PR #{pull['number']} is missing the autorelease: pending label")
    if not str(pull.get("title", "")).startswith("chore(main): release "):
        raise PromotionError(f"Release PR #{pull['number']} has an unexpected title: {pull.get('title')!r}")

    head_ref = str((pull.get("head") or {}).get("ref", ""))
    compare_contains(client, CANDIDATE_BRANCH, head_ref, client.ref_sha(CANDIDATE_BRANCH))
    compare_contains(client, staging_sha, head_ref, staging_sha)
    compare_contains(client, main_sha, head_ref, main_sha)

    client.set_draft(pull, True)
    pull["draft"] = True
    body = str(pull.get("body") or "")
    if RELEASE_PR_MARKER not in body:
        body = RELEASE_PR_INTRO + body
    updated = client.api(
        "PATCH",
        f"repos/{client.repo}/pulls/{pull['number']}",
        {"base": "main", "body": body},
    )

    write_output("has_release_pr", "true")
    write_output("pr_number", str(updated["number"]))
    write_output("pr_url", str(updated["html_url"]))
    write_output("head_ref", head_ref)
    append_summary(
        "## Release promotion\n\n"
        f"- PR: [#{updated['number']}]({updated['html_url']})\n"
        f"- Staging revision: `{staging_sha}`\n"
        f"- Main baseline: `{main_sha}`\n"
        "- State: draft until staging workflows pass"
    )


def latest_runs_by_workflow(runs: Iterable[dict[str, Any]], current_run_id: int) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if int(run.get("id", 0)) == current_run_id or run.get("name") == SELF_WORKFLOW:
            continue
        name = str(run.get("name", ""))
        if not name:
            continue
        previous = latest.get(name)
        if previous is None or int(run.get("id", 0)) > int(previous.get("id", 0)):
            latest[name] = run
    return latest


def failed_workflows(runs: dict[str, dict[str, Any]]) -> list[str]:
    failures = []
    for name, run in runs.items():
        if run.get("status") == "completed" and run.get("conclusion") not in SUCCESS_CONCLUSIONS:
            failures.append(f"{name} ({run.get('conclusion')})")
    return sorted(failures)


def wait_for_staging(
    client: GitHubClient,
    staging_sha: str,
    pull_number: int,
    current_run_id: int,
    timeout_seconds: int,
    poll_seconds: int,
    discovery_seconds: int,
) -> None:
    started_at = time.monotonic()
    latest: dict[str, dict[str, Any]] = {}
    while True:
        query = urlencode({"head_sha": staging_sha, "event": "push", "per_page": 100})
        response = client.api("GET", f"repos/{client.repo}/actions/runs?{query}")
        latest = latest_runs_by_workflow(response.get("workflow_runs", []), current_run_id)

        failures = failed_workflows(latest)
        if failures:
            raise PromotionError("Staging validation failed: " + ", ".join(failures))

        missing = REQUIRED_STAGING_WORKFLOWS - latest.keys()
        incomplete = sorted(name for name, run in latest.items() if run.get("status") != "completed")
        elapsed = time.monotonic() - started_at
        if elapsed >= discovery_seconds and not missing and not incomplete:
            break
        if elapsed >= timeout_seconds:
            details = []
            if missing:
                details.append("missing=" + ", ".join(sorted(missing)))
            if incomplete:
                details.append("incomplete=" + ", ".join(incomplete))
            raise PromotionError("Timed out waiting for staging workflows: " + "; ".join(details))
        time.sleep(poll_seconds)

    current_staging_sha = client.ref_sha("staging")
    if current_staging_sha != staging_sha:
        raise PromotionError(f"staging moved from {staging_sha} to {current_staging_sha}; keeping the PR in draft")

    pull = client.api("GET", f"repos/{client.repo}/pulls/{pull_number}")
    if str((pull.get("base") or {}).get("ref", "")) != "main":
        raise PromotionError(f"Release PR #{pull_number} no longer targets main")
    head_ref = str((pull.get("head") or {}).get("ref", ""))
    compare_contains(client, staging_sha, head_ref, staging_sha)

    workflow_lines = "\n".join(f"  - {name}: `{run.get('conclusion')}`" for name, run in sorted(latest.items()))
    append_summary(
        "## Staging validation\n\n"
        f"Release PR #{pull_number} passed staging validation and remains draft while its lockfile is refreshed.\n\n"
        f"{workflow_lines}"
    )


def update_file(client: GitHubClient, branch: str, file_path: str, source_path: str, message: str) -> None:
    path = Path(source_path)
    local_content = path.read_bytes()
    endpoint = f"repos/{client.repo}/contents/{quote(file_path, safe='/')}"
    query_endpoint = f"{endpoint}?{urlencode({'ref': branch})}"
    remote = client.api("GET", query_endpoint)
    remote_content = base64.b64decode(str(remote["content"]).replace("\n", ""))
    if remote_content == local_content:
        write_output("changed", "false")
        return

    result = client.api(
        "PUT",
        endpoint,
        {
            "message": message,
            "content": base64.b64encode(local_content).decode("ascii"),
            "sha": str(remote["sha"]),
            "branch": branch,
        },
    )
    write_output("changed", "true")
    write_output("commit_sha", str(result["commit"]["sha"]))


def back_merge(client: GitHubClient, base: str, head: str) -> None:
    merge = client.api(
        "POST",
        f"repos/{client.repo}/merges",
        {
            "base": base,
            "head": head,
            "commit_message": f"chore(release): back-merge {head} into {base}",
        },
        allow_merge_blocked=True,
    )
    if merge is MERGE_BLOCKED:
        owner = client.repo.split("/", 1)[0]
        query = urlencode({"state": "open", "base": base, "head": f"{owner}:{head}"})
        pulls = client.api("GET", f"repos/{client.repo}/pulls?{query}") or []
        if pulls:
            pull = pulls[0]
        else:
            pull = client.api(
                "POST",
                f"repos/{client.repo}/pulls",
                {
                    "base": base,
                    "head": head,
                    "title": f"chore(release): back-merge {head} into {base}",
                    "body": (
                        f"Le merge automatique de `{head}` dans `{base}` a été refusé"
                        " (conflit ou protection de branche). Résoudre les conflits puis"
                        " merger cette PR avec un merge commit pour resynchroniser les"
                        " fichiers de release (version, changelog, uv.lock)."
                    ),
                },
            )
        write_output("back_merge", "pull-request")
        write_output("back_merge_pr_url", str(pull["html_url"]))
        append_summary(
            f"## Back-merge\n\nDirect merge of `{head}` into `{base}` was rejected; opened or reused [{pull['html_url']}]({pull['html_url']})."
        )
        return
    if merge is None:
        write_output("back_merge", "up-to-date")
        append_summary(f"## Back-merge\n\n`{base}` already contains `{head}`; nothing to merge.")
        return
    write_output("back_merge", "merged")
    write_output("back_merge_sha", str(merge["sha"]))
    append_summary(f"## Back-merge\n\nMerged `{head}` into `{base}`: `{merge['sha']}`.")


def refresh_pull_request_checks(client: GitHubClient, pull_number: int, staging_sha: str) -> None:
    current_staging_sha = client.ref_sha("staging")
    if current_staging_sha != staging_sha:
        raise PromotionError(f"staging moved from {staging_sha} to {current_staging_sha}; keeping the PR in draft")

    pull = client.api("GET", f"repos/{client.repo}/pulls/{pull_number}")
    if str((pull.get("base") or {}).get("ref", "")) != "main":
        raise PromotionError(f"Release PR #{pull_number} no longer targets main")
    head_ref = str((pull.get("head") or {}).get("ref", ""))
    if not head_ref.startswith(RELEASE_BRANCH_PREFIX):
        raise PromotionError(f"Release PR #{pull_number} has unexpected head {head_ref!r}")
    compare_contains(client, staging_sha, head_ref, staging_sha)
    labels = {str(label.get("name", "")) for label in pull.get("labels", [])}
    if "autorelease: pending" not in labels:
        raise PromotionError(f"Release PR #{pull_number} is not pending")

    client.set_draft(pull, True)
    pull["draft"] = True
    client.set_draft(pull, False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "finalize", "wait", "update-file", "refresh-checks", "back-merge"),
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--staging-sha", default="")
    parser.add_argument("--main-sha", default="")
    parser.add_argument("--pull-number", type=int)
    parser.add_argument("--branch", default="")
    parser.add_argument("--base", default="dev")
    parser.add_argument("--head", default="main")
    parser.add_argument("--file", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--run-id", type=int, default=int(os.environ.get("GITHUB_RUN_ID", "0")))
    parser.add_argument("--timeout-seconds", type=int, default=10_800)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--discovery-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.repo:
        raise PromotionError("--repo or GITHUB_REPOSITORY is required")
    client = GitHubClient(args.repo)

    if args.command == "prepare":
        prepare(client)
    elif args.command == "finalize":
        if not args.staging_sha or not args.main_sha:
            raise PromotionError("finalize requires --staging-sha and --main-sha")
        finalize(client, args.staging_sha, args.main_sha)
    elif args.command == "wait":
        if not args.staging_sha or args.pull_number is None:
            raise PromotionError("wait requires --staging-sha and --pull-number")
        wait_for_staging(
            client,
            args.staging_sha,
            args.pull_number,
            args.run_id,
            args.timeout_seconds,
            args.poll_seconds,
            args.discovery_seconds,
        )
    elif args.command == "update-file":
        if not args.branch or not args.file or not args.source or not args.message:
            raise PromotionError("update-file requires --branch, --file, --source and --message")
        update_file(client, args.branch, args.file, args.source, args.message)
    elif args.command == "back-merge":
        if not args.base or not args.head:
            raise PromotionError("back-merge requires --base and --head")
        back_merge(client, args.base, args.head)
    else:
        if args.pull_number is None or not args.staging_sha:
            raise PromotionError("refresh-checks requires --pull-number and --staging-sha")
        refresh_pull_request_checks(client, args.pull_number, args.staging_sha)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PromotionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
