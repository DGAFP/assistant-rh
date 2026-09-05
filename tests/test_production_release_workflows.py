import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_production_migrations_follow_successful_release_please_on_main() -> None:
    workflow = (WORKFLOWS / "db-migrations-scaleway.yml").read_text(encoding="utf-8")
    trigger_block = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    production_job = workflow.split("migrate-production:", 1)[1]

    assert 'workflows:\n      - "Release Please"' in trigger_block
    assert "branches:\n      - main" in trigger_block
    assert "release:" not in trigger_block
    assert "github.event.workflow_run.conclusion == 'success'" in production_job
    assert "github.event.workflow_run.event == 'push'" in production_job
    assert "github.event.workflow_run.head_branch == 'main'" in production_job
    assert "ref: ${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.ref }}" in production_job
    assert "group: scaleway-production-db-migrations" in production_job
    assert 'MAIN_SHA="$(git ls-remote origin refs/heads/main | cut -f1)"' in production_job


def test_production_release_chain_is_serialized_through_deployment() -> None:
    workflow = (WORKFLOWS / "db-migrations-scaleway.yml").read_text(encoding="utf-8")
    workflow_concurrency = workflow.split("permissions:", 1)[1].split("jobs:", 1)[0]

    assert (
        "group: ${{ (github.event_name == 'workflow_run' || inputs.target == 'production') "
        "&& 'scaleway-production-release-chain'"
    ) in workflow_concurrency
    assert "cancel-in-progress: false" in workflow_concurrency
    assert "deploy-production:" in workflow
    assert "needs: migrate-production" in workflow


def test_production_migrations_require_a_published_semantic_release() -> None:
    workflow = (WORKFLOWS / "db-migrations-scaleway.yml").read_text(encoding="utf-8")
    release_step = workflow.split("- name: Resolve published release", 1)[1].split("- name: Setup Supabase CLI", 1)[0]
    resolver = (REPO_ROOT / ".github" / "scripts" / "resolve_published_release.sh").read_text(encoding="utf-8")

    assert "resolve_published_release.sh" in release_step
    assert "git tag --points-at" in resolver
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in resolver
    assert 'gh api "repos/${GITHUB_REPOSITORY}/releases/tags/${RELEASE_TAG}"' in resolver
    assert ".draft == false and .prerelease == false" in resolver


def test_streamlit_deploy_receives_the_exact_migrated_release() -> None:
    migrations = (WORKFLOWS / "db-migrations-scaleway.yml").read_text(encoding="utf-8")
    workflow = (WORKFLOWS / "streamlit-deploy-production.yml").read_text(encoding="utf-8")
    trigger_block = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    deploy_job = workflow.split("deploy:", 1)[1]

    assert "workflow_call:" in trigger_block
    assert "workflow_run:" not in trigger_block
    assert "uses: ./.github/workflows/streamlit-deploy-production.yml" in migrations
    assert "release_sha: ${{ github.event.workflow_run.head_sha }}" in migrations
    assert "release_tag: ${{ needs.migrate-production.outputs.release_tag }}" in migrations
    assert "ref: ${{ inputs.release_sha || github.ref }}" in deploy_job
    assert 'if [[ -n "${RELEASE_WORKFLOW_SHA}" || -n "${RELEASE_WORKFLOW_TAG}" ]]' in deploy_job
    assert "github.event_name == 'workflow_call'" not in deploy_job
    assert "resolve_published_release.sh" in deploy_job
    assert 'if [[ "${RELEASE_TAG}" != "${RELEASE_WORKFLOW_TAG}" ]]' in deploy_job
    assert "WORKFLOW_RUN_DISPLAY_TITLE" not in deploy_job


def _commit_release(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "README.md").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=repository, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    return repository, sha


def _run_resolver(tmp_path: Path, repository: Path, sha: str, published_tag: str = "v1.2.3") -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$PUBLISHED_TAG\"\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    env = os.environ | {
        "GITHUB_REPOSITORY": "DGAFP/assistant-rh",
        "PUBLISHED_TAG": published_tag,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    resolver = REPO_ROOT / ".github" / "scripts" / "resolve_published_release.sh"
    return subprocess.run(["bash", resolver, sha], cwd=repository, env=env, capture_output=True, text=True)


def test_release_resolver_accepts_the_exact_published_tag(tmp_path: Path) -> None:
    repository, sha = _commit_release(tmp_path)
    subprocess.run(["git", "tag", "v1.2.3"], cwd=repository, check=True)

    result = _run_resolver(tmp_path, repository, sha)

    assert result.returncode == 0
    assert result.stdout.strip() == "v1.2.3"


def test_release_resolver_rejects_missing_or_unpublished_tags(tmp_path: Path) -> None:
    repository, sha = _commit_release(tmp_path)
    missing = _run_resolver(tmp_path, repository, sha)
    subprocess.run(["git", "tag", "v1.2.3"], cwd=repository, check=True)
    unpublished = _run_resolver(tmp_path, repository, sha, published_tag="")

    assert missing.returncode != 0
    assert "No semantic release tag" in missing.stderr
    assert unpublished.returncode != 0
    assert "missing, draft, or a prerelease" in unpublished.stderr


def test_release_resolver_rejects_invalid_mismatched_and_ambiguous_releases(tmp_path: Path) -> None:
    repository, sha = _commit_release(tmp_path)
    subprocess.run(["git", "tag", "v1.2.3"], cwd=repository, check=True)
    invalid = _run_resolver(tmp_path, repository, "not-a-sha")
    mismatched = _run_resolver(tmp_path, repository, "0" * 40)
    subprocess.run(["git", "tag", "v1.2.4"], cwd=repository, check=True)
    ambiguous = _run_resolver(tmp_path, repository, sha)

    assert invalid.returncode != 0
    assert "Invalid release SHA" in invalid.stderr
    assert mismatched.returncode != 0
    assert "does not match release SHA" in mismatched.stderr
    assert ambiguous.returncode != 0
    assert "Multiple semantic release tags" in ambiguous.stderr
