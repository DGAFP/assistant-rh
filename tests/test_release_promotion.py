from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github/scripts/release_promotion.py"
SPEC = importlib.util.spec_from_file_location("release_promotion", SCRIPT_PATH)
assert SPEC and SPEC.loader
release_promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_promotion)


def _pull(number: int, head: str, *, repo: str = "DGAFP/assistant-rh", base: str = "main") -> dict:
    return {
        "number": number,
        "head": {"ref": head, "repo": {"full_name": repo}},
        "base": {"ref": base},
    }


def test_release_pull_requests_only_selects_candidate_branch_from_same_repo() -> None:
    pulls = [
        _pull(1, "release-please--branches--release-candidate--components--assistant-rh"),
        _pull(2, "release-please--branches--main--components--assistant-rh"),
        _pull(3, "release-please--branches--release-candidate--components--assistant-rh", repo="fork/assistant-rh"),
    ]

    selected = release_promotion.release_pull_requests(pulls, "DGAFP/assistant-rh")

    assert [pull["number"] for pull in selected] == [1]


def test_latest_runs_ignores_current_release_workflow_and_uses_latest_attempt() -> None:
    runs = [
        {"id": 10, "name": "CI Tests", "status": "completed", "conclusion": "failure"},
        {"id": 11, "name": "CI Tests", "status": "completed", "conclusion": "success"},
        {"id": 12, "name": "Release Please", "status": "in_progress", "conclusion": None},
        {"id": 13, "name": "CodeQL", "status": "completed", "conclusion": "success"},
    ]

    latest = release_promotion.latest_runs_by_workflow(runs, current_run_id=12)

    assert latest["CI Tests"]["id"] == 11
    assert set(latest) == {"CI Tests", "CodeQL"}
    assert release_promotion.failed_workflows(latest) == []


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_failed_workflows_rejects_non_successful_conclusions(conclusion: str) -> None:
    runs = {"Conformance": {"status": "completed", "conclusion": conclusion}}

    assert release_promotion.failed_workflows(runs) == [f"Conformance ({conclusion})"]


class _FakeClient:
    repo = "DGAFP/assistant-rh"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.draft_updates: list[tuple[int, bool]] = []
        self.pull = {
            **_pull(42, "release-please--branches--release-candidate--components--assistant-rh", base="release-candidate"),
            "node_id": "PR_node",
            "draft": True,
            "title": "chore(main): release 0.9.0",
            "body": "Release notes",
            "labels": [{"name": "autorelease: pending"}],
            "html_url": "https://github.com/DGAFP/assistant-rh/pull/42",
        }
        self.remote_file_content = b"old lock"
        self.merge_result: object = {"sha": "candidate-sha"}
        self.open_back_merge_pulls: list[dict] = []

    def ref_sha(self, branch: str) -> str:
        return {"main": "main-sha", "staging": "staging-sha", "release-candidate": "candidate-sha"}[branch]

    def api(
        self,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        *,
        allow_not_found: bool = False,
        allow_merge_blocked: bool = False,
    ):
        self.calls.append((method, endpoint, payload))
        if endpoint.endswith("/git/ref/heads/release-candidate"):
            return None if allow_not_found else {"object": {"sha": "candidate-sha"}}
        if endpoint.endswith("/merges"):
            if self.merge_result is release_promotion.MERGE_BLOCKED:
                assert allow_merge_blocked
            return self.merge_result
        if endpoint.endswith("/pulls?state=open&per_page=100"):
            return [self.pull]
        if "/pulls?" in endpoint and "base=dev" in endpoint:
            return self.open_back_merge_pulls
        if endpoint.endswith("/pulls") and method == "POST":
            return {
                "number": 99,
                "html_url": "https://github.com/DGAFP/assistant-rh/pull/99",
            }
        if "/contents/uv.lock?" in endpoint:
            return {
                "sha": "blob-sha",
                "content": base64.b64encode(self.remote_file_content).decode("ascii"),
            }
        if endpoint.endswith("/contents/uv.lock") and method == "PUT":
            self.remote_file_content = base64.b64decode(str(payload["content"]))
            return {"commit": {"sha": "lock-commit-sha"}}
        if "/compare/" in endpoint:
            base = endpoint.split("/compare/", 1)[1].split("...", 1)[0]
            expected_sha = {
                "release-candidate": "candidate-sha",
                "staging-sha": "staging-sha",
                "main-sha": "main-sha",
            }[base]
            return {
                "status": "ahead",
                "merge_base_commit": {"sha": expected_sha},
            }
        if endpoint.endswith("/pulls/42") and method == "PATCH":
            self.pull["base"] = {"ref": str(payload["base"])}
            self.pull["body"] = str(payload.get("body", self.pull["body"]))
            return self.pull
        if endpoint.endswith("/pulls/42"):
            return self.pull
        if "/actions/runs?" in endpoint:
            runs = [
                {"id": index, "name": name, "status": "completed", "conclusion": "success"}
                for index, name in enumerate(sorted(release_promotion.REQUIRED_STAGING_WORKFLOWS), start=100)
            ]
            return {"workflow_runs": runs}
        return {}

    def set_draft(self, pull_request: dict, draft: bool) -> None:
        self.draft_updates.append((int(pull_request["number"]), draft))
        pull_request["draft"] = draft


def test_prepare_rebuilds_candidate_and_moves_existing_pr_back_for_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.pull["base"] = {"ref": "main"}
    client.pull["draft"] = False
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.prepare(client)

    assert client.draft_updates == [(42, True)]
    assert any(method == "POST" and endpoint.endswith("/merges") for method, endpoint, _ in client.calls)
    assert any(payload == {"base": "release-candidate"} for method, endpoint, payload in client.calls if method == "PATCH" and "/pulls/" in endpoint)
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs == {
        "main_sha": "main-sha",
        "staging_sha": "staging-sha",
        "candidate_sha": "candidate-sha",
        "existing_pr_number": "42",
    }


def test_finalize_retargets_release_pr_and_adds_promotion_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.pull["draft"] = False
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.finalize(client, "staging-sha", "main-sha")

    assert client.pull["base"]["ref"] == "main"
    assert client.draft_updates == [(42, True)]
    assert release_promotion.RELEASE_PR_MARKER in client.pull["body"]
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs["has_release_pr"] == "true"
    assert outputs["pr_number"] == "42"


def test_wait_validates_required_staging_workflows_while_keeping_pr_draft() -> None:
    client = _FakeClient()
    client.pull["base"] = {"ref": "main"}

    release_promotion.wait_for_staging(
        client,
        "staging-sha",
        pull_number=42,
        current_run_id=999,
        timeout_seconds=1,
        poll_seconds=0,
        discovery_seconds=0,
    )

    assert client.draft_updates == []


def test_update_file_uses_contents_api_without_custom_commit_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    lockfile = tmp_path / "uv.lock"
    lockfile.write_bytes(b"new lock")
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(tmp_path)

    release_promotion.update_file(client, "release-branch", "uv.lock", "uv.lock", "chore: update lock")

    put_call = next(call for call in client.calls if call[0] == "PUT")
    assert set(put_call[2]) == {"message", "content", "sha", "branch"}
    assert client.remote_file_content == b"new lock"
    assert "changed=true" in output.read_text(encoding="utf-8")


def test_refresh_checks_toggles_release_pr_from_draft_to_ready() -> None:
    client = _FakeClient()
    client.pull["base"] = {"ref": "main"}
    client.pull["draft"] = False

    release_promotion.refresh_pull_request_checks(client, 42, "staging-sha")

    assert client.draft_updates == [(42, True), (42, False)]


def test_back_merge_merges_main_into_dev_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.merge_result = {"sha": "back-merge-sha"}
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.back_merge(client, "dev", "main")

    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs == {"back_merge": "merged", "back_merge_sha": "back-merge-sha"}


def test_back_merge_is_a_noop_when_dev_already_contains_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.merge_result = None
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.back_merge(client, "dev", "main")

    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs == {"back_merge": "up-to-date"}
    assert not any(method == "POST" and endpoint.endswith("/pulls") for method, endpoint, _ in client.calls)


def test_back_merge_opens_pull_request_when_direct_merge_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.merge_result = release_promotion.MERGE_BLOCKED
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.back_merge(client, "dev", "main")

    create_call = next(call for call in client.calls if call[0] == "POST" and call[1].endswith("/pulls"))
    assert create_call[2]["base"] == "dev"
    assert create_call[2]["head"] == "main"
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs["back_merge"] == "pull-request"
    assert outputs["back_merge_pr_url"].endswith("/pull/99")


def test_back_merge_reuses_existing_back_merge_pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client.merge_result = release_promotion.MERGE_BLOCKED
    client.open_back_merge_pulls = [{"number": 7, "html_url": "https://github.com/DGAFP/assistant-rh/pull/7"}]
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    release_promotion.back_merge(client, "dev", "main")

    assert not any(method == "POST" and endpoint.endswith("/pulls") for method, endpoint, _ in client.calls)
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert outputs["back_merge_pr_url"].endswith("/pull/7")


def test_workflow_creates_one_draft_promotion_and_publishes_without_second_pr() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release-please.yml").read_text(encoding="utf-8")
    config = (REPO_ROOT / "release-please-config.json").read_text(encoding="utf-8")

    assert "- staging" in workflow
    assert "target-branch: release-candidate" in workflow
    assert "skip-github-release: true" in workflow
    assert "Retarget release PR to main" in workflow
    assert "Wait for staging workflows" in workflow
    assert "Commit signed lockfile update through GitHub" in workflow
    assert "Mark release PR ready and trigger main checks" in workflow
    assert "target-branch: main" in workflow
    assert "skip-github-pull-request: true" in workflow
    assert '"draft-pull-request": true' in config
    assert '"pull-request-title-pattern": "chore(main): release ${version}"' in config

    assert not (REPO_ROOT / ".github/workflows/release-please-lockfile.yml").exists()
    assert "release_promotion.py update-file" in workflow
    assert "release_promotion.py refresh-checks" in workflow
    assert "Back-merge main into dev" in workflow
    assert "release_promotion.py back-merge --base dev --head main" in workflow


@pytest.mark.parametrize(
    "workflow_path",
    [
        ".github/workflows/ci-tests.yml",
        ".github/workflows/codeql.yml",
        ".github/workflows/data-engineering-ci.yml",
    ],
)
def test_main_pr_checks_run_when_release_pr_becomes_ready(workflow_path: str) -> None:
    workflow = (REPO_ROOT / workflow_path).read_text(encoding="utf-8")

    assert "ready_for_review" in workflow
