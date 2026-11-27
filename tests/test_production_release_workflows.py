import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_migrations_do_not_follow_a_merge_release_or_tag_automatically() -> None:
    workflow = (WORKFLOWS / "db-migrations-scaleway.yml").read_text(encoding="utf-8")
    trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    production = workflow.split("migrate-production:", 1)[1]
    assert "workflow_run:" not in trigger
    assert "release:" not in trigger
    assert "workflow_dispatch:" in trigger
    assert "github.event_name == 'workflow_dispatch' && inputs.target == 'production'" in production
    assert "deploy-production:" not in workflow
    assert "uses: ./.github/workflows/streamlit-deploy-production.yml" not in workflow
    assert "group: scaleway-production-db-migrations" in production


def test_tagged_delivery_is_serialized_and_requires_private_source_qualification() -> None:
    workflow = (WORKFLOWS / "tagged-release.yml").read_text(encoding="utf-8")
    assert "group: assistant-rh-api-tagged-delivery" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "needs: [plan, qualify-source]" in workflow
    assert "environment: scaleway-staging" in workflow
    assert "HF_CONFORMANCE_TOKEN" in workflow
    assert "ref: ${{ needs.plan.outputs.source_sha }}" in workflow
    assert "test_m0b_companion.py" in workflow
    assert "Publish and wait for observed deployment and recipe proof" in workflow


def test_main_does_not_publish_release_tags_or_back_merge_to_dev() -> None:
    assert not (WORKFLOWS / "release-please.yml").exists()
    workflow = (WORKFLOWS / "tagged-release.yml").read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "TAGGED_RELEASES_ENABLED == 'true'" in workflow
    validation = (WORKFLOWS / "kapsule-staging.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in validation
    assert "tags: ['v*']" in validation
    assert "SCW_REGISTRY_SECRET_KEY" not in validation
    assert "docker push" not in validation


def test_legacy_streamlit_production_is_only_callable_or_manual() -> None:
    workflow = (WORKFLOWS / "streamlit-deploy-production.yml").read_text(encoding="utf-8")
    trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "workflow_call:" in trigger
    assert "workflow_dispatch:" in trigger
    assert "workflow_run:" not in trigger
    assert "release:" not in trigger
    assert "resolve_published_release.sh" in workflow
    assert "ref: ${{ inputs.release_sha || github.ref }}" in workflow


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
