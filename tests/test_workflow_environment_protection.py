from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_data_preview_pr_never_enters_staging_environment() -> None:
    workflow = (REPO_ROOT / ".github/workflows/data-engineering-preview-staging.yml").read_text(encoding="utf-8")
    build_job_header = workflow.split("  build-staging-images:", 1)[1].split("    strategy:", 1)[0]

    assert "github.event_name != 'pull_request'" in build_job_header
    assert "environment: scaleway-staging" in build_job_header


def test_conformance_uses_secrets_only_after_staging_integration() -> None:
    workflow = (REPO_ROOT / ".github/workflows/conformance.yml").read_text(encoding="utf-8")
    triggers = workflow.split("permissions:", 1)[0]
    stage_job_header = workflow.split("  stage-conformance:", 1)[1].split("    strategy:", 1)[0]
    summary_job = workflow.split("  summary:", 1)[1]

    assert "pull_request:" in triggers
    assert "push:" in triggers
    assert "branches: [staging]" in triggers
    assert "github.event_name != 'pull_request'" in stage_job_header
    assert "environment: scaleway-staging" in stage_job_header
    assert "Build protected-ref PR summary" in summary_job
    assert "Secret-backed replay is deferred" in summary_job
