"""Offline unit tests for data-engineering CI helpers.

These tests document edge cases for Git change planning and Scaleway command
rendering without calling real GitHub Actions or Scaleway services.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import data_engineering_plan  # noqa: E402
import scaleway_data_jobs  # noqa: E402


def test_classify_from_files_selects_only_changed_data_domains() -> None:
    selected = data_engineering_plan.classify_from_files(
        [
            "packages/data-engineering/src/assistant_rh_data_engineering/service_public/silver.py",
            "config/legifrance_articles.json",
            "docs/PIPELINE.md",
        ]
    )

    assert selected == {
        "service_public": True,
        "legifrance": True,
        "embeddings": False,
    }


def test_classify_from_files_common_ci_change_selects_all_domains() -> None:
    selected = data_engineering_plan.classify_from_files([".github/scripts/scaleway_data_jobs.py"])

    assert selected == {
        "service_public": True,
        "legifrance": True,
        "embeddings": True,
    }


def test_changed_files_falls_back_to_all_files_when_git_diff_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "HEAD^"):
            return "base-sha"
        if args == ("diff", "--name-only", "base-sha", "HEAD"):
            raise data_engineering_plan.subprocess.CalledProcessError(1, ["git", *args])
        if args == ("ls-files",):
            return "Dockerfile.embeddings_job\nREADME.md\n"
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.delenv("GITHUB_EVENT_BEFORE", raising=False)
    monkeypatch.delenv("BEFORE", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(data_engineering_plan, "git_output", fake_git_output)

    assert data_engineering_plan.changed_files() == ["Dockerfile.embeddings_job", "README.md"]


def test_workflow_dispatch_main_writes_legifrance_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("INPUT_SOURCE", "legifrance")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    assert data_engineering_plan.main() == 0

    outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())
    matrix = json.loads(outputs["matrix"])
    assert outputs["service_public"] == "false"
    assert outputs["legifrance"] == "true"
    assert outputs["embeddings"] == "false"
    assert outputs["has_builds"] == "true"
    assert [item["image"] for item in matrix["include"]] == [
        "legifrance-bulk-dump",
        "legifrance-pipeline",
        "legifrance-ingestion",
    ]


def test_scaleway_job_environment_resolves_required_env_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCW_ACCESS_KEY", "access-key")
    monkeypatch.setenv("SCW_SECRET_KEY", "secret-key")
    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://db")
    monkeypatch.setenv("SCALEWAY_API_KEY", "api-key")
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://api.example.test")

    environment = scaleway_data_jobs.job_environment(
        {"env_groups": ["object_storage", "postgres", "embeddings_api"]},
        "staging",
        "fr-par",
    )

    assert environment["SCW_ACCESS_KEY"] == "access-key"
    assert environment["SCW_SECRET_KEY"] == "secret-key"
    assert environment["SCW_POSTGRES_DSN"] == "postgresql://db"
    assert environment["SCALEWAY_API_KEY"] == "api-key"
    assert environment["SCALEWAY_BASE_URL"] == "https://api.example.test"
    assert environment["TARGET_ENV"] == "staging"


def test_scaleway_job_environment_defaults_embeddings_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "api-key")
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)

    environment = scaleway_data_jobs.job_environment(
        {"env_groups": ["embeddings_api"]},
        "staging",
        "fr-par",
    )

    assert environment["SCALEWAY_API_KEY"] == "api-key"
    assert environment["SCALEWAY_BASE_URL"] == "https://api.scaleway.ai/v1"


def test_scaleway_job_environment_fails_on_missing_required_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCW_ACCESS_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SCW_ACCESS_KEY"):
        scaleway_data_jobs.job_environment({"env_groups": ["object_storage"]}, "prod", "fr-par")


def test_run_scw_dry_run_redacts_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    output = scaleway_data_jobs.run_scw(
        ["jobs", "definition", "start", "job-id", "environment-variables.SECRET=plain-secret"],
        secrets=["plain-secret"],
        dry_run=True,
    )

    captured = capsys.readouterr()
    assert output == "{}"
    assert "plain-secret" not in captured.out
    assert "environment-variables.SECRET=***" in captured.out


def test_run_scw_failure_redacts_secrets_in_output_and_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(*args, **kwargs):
        return scaleway_data_jobs.subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="stdout has plain-secret",
            stderr="stderr has plain-secret",
        )

    monkeypatch.setattr(scaleway_data_jobs.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        scaleway_data_jobs.run_scw(
            ["jobs", "definition", "start", "job-id", "environment-variables.SECRET=plain-secret"],
            secrets=["plain-secret"],
            dry_run=False,
        )

    captured = capsys.readouterr()
    assert "plain-secret" not in captured.out
    assert "plain-secret" not in str(exc_info.value)
    assert "stdout has ***" in captured.out
    assert "stderr has ***" in captured.out
    assert "environment-variables.SECRET=***" in str(exc_info.value)


def test_start_definition_renders_scw_start_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run_scw(args: list[str], *, secrets: list[str], dry_run: bool = False) -> str:
        calls.append(args)
        assert secrets == ["secret"]
        assert dry_run is True
        return "{}"

    monkeypatch.setattr(scaleway_data_jobs, "run_scw", fake_run_scw)

    scaleway_data_jobs.start_definition(
        "job-id",
        {"key": "service-public-medallion"},
        ["service-public", "medallion", "--target-env", "staging"],
        {"TARGET_ENV": "staging", "SCW_SECRET_KEY": "secret"},
        "fr-par",
        wait=True,
        secrets=["secret"],
        dry_run=True,
    )

    assert calls == [
        [
            "jobs",
            "definition",
            "start",
            "job-id",
            "startup-command.0=data-ingestion",
            "args.0=service-public",
            "args.1=medallion",
            "args.2=--target-env",
            "args.3=staging",
            "environment-variables.SCW_SECRET_KEY=secret",
            "environment-variables.TARGET_ENV=staging",
            "region=fr-par",
            "-w",
        ]
    ]


def test_upsert_and_start_jobs_uses_existing_definition_without_real_scw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "jobs.json"
    config_path.write_text(
        json.dumps(
            {
                "job_name_template": "assistant-rh-{target_env}-{key}",
                "jobs": [
                    {
                        "key": "service-public-medallion",
                        "domain": "service_public",
                        "image": "service-public-pipeline",
                        "description": "Pipeline",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "env_groups": [],
                        "args": ["service-public", "medallion", "--target-env", "{target_env}"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    updated: list[tuple[str, str]] = []
    started: list[list[str]] = []

    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "project-id")
    monkeypatch.delenv("SCW_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("SCW_CONTAINER_REGISTRY_NAMESPACE", raising=False)
    monkeypatch.setattr(
        scaleway_data_jobs,
        "list_definitions",
        lambda project_id, region, *, secrets, dry_run: {"assistant-rh-staging-service-public-medallion": {"id": "existing-id"}},
    )
    monkeypatch.setattr(
        scaleway_data_jobs,
        "update_definition",
        lambda spec, job_id, image, region, *, secrets, dry_run: updated.append((job_id, image)),
    )
    monkeypatch.setattr(
        scaleway_data_jobs,
        "start_definition",
        lambda job_id, spec, command_args, environment, region, *, wait, secrets, dry_run: started.append(command_args),
    )

    args = SimpleNamespace(
        config=str(config_path),
        target_env="staging",
        image_tag="sha-123",
        service_public=True,
        legifrance=False,
        embeddings=False,
        run_ingestion=False,
        run_embeddings=False,
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    assert scaleway_data_jobs.upsert_and_start_jobs(args) == 0

    assert updated == [("existing-id", "rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:sha-123")]
    assert started == [["service-public", "medallion", "--target-env", "staging"]]


def test_upsert_and_start_jobs_appends_prod_args_for_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "jobs.json"
    config_path.write_text(
        json.dumps(
            {
                "job_name_template": "assistant-rh-{target_env}-{key}",
                "jobs": [
                    {
                        "key": "legifrance-bulk-dump",
                        "domain": "legifrance",
                        "image": "legifrance-bulk-dump",
                        "description": "Bulk dump",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "env_groups": [],
                        "args": ["legifrance", "bulk-dump", "--target-env", "{target_env}"],
                        "prod_args": ["--delete-remote"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    started: list[list[str]] = []

    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "project-id")
    monkeypatch.delenv("SCW_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("SCW_CONTAINER_REGISTRY_NAMESPACE", raising=False)
    monkeypatch.setattr(scaleway_data_jobs, "list_definitions", lambda project_id, region, *, secrets, dry_run: {})
    monkeypatch.setattr(scaleway_data_jobs, "create_definition", lambda spec, name, image, project_id, region, *, secrets, dry_run: "new-id")
    monkeypatch.setattr(
        scaleway_data_jobs,
        "start_definition",
        lambda job_id, spec, command_args, environment, region, *, wait, secrets, dry_run: started.append(command_args),
    )

    args = SimpleNamespace(
        config=str(config_path),
        target_env="prod",
        image_tag="release-1",
        service_public=False,
        legifrance=True,
        embeddings=False,
        run_ingestion=False,
        run_embeddings=False,
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    assert scaleway_data_jobs.upsert_and_start_jobs(args) == 0

    assert started == [["legifrance", "bulk-dump", "--target-env", "prod", "--delete-remote"]]
