from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_data_preview_pr_never_enters_staging_environment() -> None:
    workflow = (REPO_ROOT / ".github/workflows/data-engineering-preview-staging.yml").read_text(encoding="utf-8")
    build_job_header = workflow.split("  build-staging-images:", 1)[1].split("    strategy:", 1)[0]

    assert "github.event_name != 'pull_request'" in build_job_header
    assert "environment: scaleway-staging" in build_job_header
