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
import scaleway_rag_health_deploy  # noqa: E402


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
    assert outputs["run_embeddings"] == "false"
    assert outputs["has_builds"] == "true"
    assert [item["image"] for item in matrix["include"]] == [
        "legifrance-bulk-dump",
        "legifrance-pipeline",
        "legifrance-ingestion",
    ]


def test_workflow_dispatch_run_embeddings_adds_embeddings_to_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("INPUT_SOURCE", "service_public")
    monkeypatch.setenv("INPUT_RUN_EMBEDDINGS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    assert data_engineering_plan.main() == 0

    outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())
    matrix = json.loads(outputs["matrix"])
    assert outputs["service_public"] == "true"
    assert outputs["legifrance"] == "false"
    assert outputs["embeddings"] == "true"
    assert outputs["run_embeddings"] == "true"
    assert [item["image"] for item in matrix["include"]] == [
        "service-public-pipeline",
        "service-public-ingestion",
        "embeddings-job",
    ]


def test_preview_staging_plan_receives_run_embeddings_input() -> None:
    workflow = (REPO_ROOT / ".github/workflows/data-engineering-preview-staging.yml").read_text(encoding="utf-8")
    plan_step = workflow.split("- name: Detect changed data engineering jobs", 1)[1].split(
        "run: python3 .github/scripts/data_engineering_plan.py",
        1,
    )[0]

    assert "INPUT_SOURCE: ${{ github.event_name == 'workflow_dispatch' && inputs.source || '' }}" in plan_step
    assert "INPUT_RUN_EMBEDDINGS: ${{ github.event_name == 'workflow_dispatch' && inputs.run_embeddings || false }}" in plan_step


def test_workflow_dispatch_all_selects_embeddings_without_running_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("INPUT_SOURCE", "all")
    monkeypatch.delenv("INPUT_RUN_EMBEDDINGS", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    assert data_engineering_plan.main() == 0

    outputs = dict(line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines())
    assert outputs["embeddings"] == "true"
    assert outputs["run_embeddings"] == "false"


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


def test_redacted_handles_overlapping_secrets_longest_first() -> None:
    secrets = ["plain-secret", "plain-secret-extended", ""]

    output = scaleway_data_jobs.redacted("token=plain-secret-extended end", secrets)

    assert output == "token=*** end"
    assert "extended" not in output


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


def test_rag_health_container_settings_use_current_scaleway_container_args() -> None:
    args = scaleway_rag_health_deploy.rag_health_container_settings_args(
        image_uri="rg.fr-par.scw.cloud/assistant-rh/rag-health-exporter:staging-sha",
        min_scale=1,
        max_scale=1,
        memory_limit_mb=1024,
        cpu_limit_milli=250,
        timeout_seconds=120,
        port=9108,
        protocol="http1",
        privacy="private",
        health_path="/healthz",
        environment={"APP_ENV": "staging"},
        secret_environment={"RAG_HEALTH_POSTGRES_DSN": "postgresql://db"},
    )

    assert "image=rg.fr-par.scw.cloud/assistant-rh/rag-health-exporter:staging-sha" in args
    assert "memory-limit-bytes=1GB" in args
    assert "mvcpu-limit=250" in args
    assert "startup-probe.http.path=/healthz" in args
    assert "liveness-probe.http.path=/healthz" in args
    assert "environment-variables.APP_ENV=staging" in args
    assert "secret-environment-variables.RAG_HEALTH_POSTGRES_DSN=postgresql://db" in args
    assert not any(arg.startswith("registry-image=") for arg in args)
    assert not any(arg.startswith("health-check.") for arg in args)


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
        return json.dumps({"id": "run-id", "state": "succeeded"})

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
            "-o",
            "json",
            "-w",
        ]
    ]


def test_start_definition_raises_when_waited_scaleway_run_failed() -> None:
    def fake_run_scw(args: list[str], *, secrets: list[str], dry_run: bool = False) -> str:
        assert "-w" in args
        return json.dumps(
            {
                "id": "run-id",
                "state": "failed",
                "reason": "exited_with_error",
                "error_message": "database password plain-secret leaked here",
            }
        )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(scaleway_data_jobs, "run_scw", fake_run_scw)
        with pytest.raises(RuntimeError) as exc_info:
            scaleway_data_jobs.start_definition(
                "job-id",
                {"key": "service-public-ingestion"},
                ["service-public", "ingest"],
                {"TARGET_ENV": "staging"},
                "fr-par",
                wait=True,
                secrets=["plain-secret"],
                dry_run=False,
            )

    message = str(exc_info.value)
    assert "service-public-ingestion" in message
    assert "state=failed" in message
    assert "exited_with_error" in message
    assert "plain-secret" not in message
    assert "***" in message


def test_start_definition_redacts_secrets_when_waited_run_output_is_malformed() -> None:
    def fake_run_scw(args: list[str], *, secrets: list[str], dry_run: bool = False) -> str:
        assert "-w" in args
        return "not-json plain-secret"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(scaleway_data_jobs, "run_scw", fake_run_scw)
        with pytest.raises(RuntimeError) as exc_info:
            scaleway_data_jobs.start_definition(
                "job-id",
                {"key": "service-public-ingestion"},
                ["service-public", "ingest"],
                {"TARGET_ENV": "staging"},
                "fr-par",
                wait=True,
                secrets=["plain-secret"],
                dry_run=False,
            )

    message = str(exc_info.value)
    assert "Unable to parse Scaleway run output" in message
    assert "plain-secret" not in message
    assert "***" in message


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


def test_upsert_and_start_jobs_appends_wipe_existing_chunks_to_service_public_ingestion(
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
                    },
                    {
                        "key": "service-public-ingestion",
                        "domain": "service_public",
                        "image": "service-public-ingestion",
                        "description": "Ingestion",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_ingestion": True,
                        "env_groups": [],
                        "args": ["service-public", "ingest", "--target-env", "{target_env}"],
                    },
                    {
                        "key": "embeddings-service-public",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Service-Public embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "service-public", "--dsn-env", "SCW_POSTGRES_DSN"],
                    },
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
        target_env="staging",
        image_tag="sha-123",
        service_public=True,
        legifrance=False,
        embeddings=True,
        run_ingestion=True,
        run_embeddings=True,
        wipe_existing_chunks=True,
        embedding_source="service_public",
        embedding_only_column="",
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    assert scaleway_data_jobs.upsert_and_start_jobs(args) == 0

    assert started == [
        ["service-public", "medallion", "--target-env", "staging"],
        ["service-public", "ingest", "--target-env", "staging", "--wipe-existing-chunks"],
        ["embeddings", "service-public", "--dsn-env", "SCW_POSTGRES_DSN"],
    ]


def test_upsert_and_start_jobs_rejects_wipe_existing_chunks_without_service_public_backfill(
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
                        "key": "service-public-ingestion",
                        "domain": "service_public",
                        "image": "service-public-ingestion",
                        "description": "Ingestion",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_ingestion": True,
                        "env_groups": [],
                        "args": ["service-public", "ingest"],
                    },
                    {
                        "key": "embeddings-service-public",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Service-Public embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "service-public"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "project-id")
    monkeypatch.setattr(scaleway_data_jobs, "list_definitions", lambda project_id, region, *, secrets, dry_run: {})

    args = SimpleNamespace(
        config=str(config_path),
        target_env="staging",
        image_tag="sha-123",
        service_public=True,
        legifrance=False,
        embeddings=False,
        run_ingestion=True,
        run_embeddings=False,
        wipe_existing_chunks=True,
        embedding_source="service_public",
        embedding_only_column="",
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    with pytest.raises(RuntimeError, match="Service-Public embeddings backfill"):
        scaleway_data_jobs.upsert_and_start_jobs(args)


def test_upsert_and_start_jobs_runs_service_public_backfill_right_after_wipe(
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
                        "args": ["service-public", "medallion"],
                    },
                    {
                        "key": "service-public-ingestion",
                        "domain": "service_public",
                        "image": "service-public-ingestion",
                        "description": "Ingestion",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_ingestion": True,
                        "env_groups": [],
                        "args": ["service-public", "ingest"],
                    },
                    {
                        "key": "legifrance-bulk-dump",
                        "domain": "legifrance",
                        "image": "legifrance-bulk-dump",
                        "description": "Legifrance dump",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "env_groups": [],
                        "args": ["legifrance", "bulk-dump"],
                    },
                    {
                        "key": "legifrance-ingestion",
                        "domain": "legifrance",
                        "image": "legifrance-ingestion",
                        "description": "Legifrance ingestion",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_ingestion": True,
                        "env_groups": [],
                        "args": ["legifrance", "ingest"],
                    },
                    {
                        "key": "embeddings-service-public",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Service-Public embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "service-public"],
                    },
                    {
                        "key": "embeddings-legifrance",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Legifrance embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "legifrance"],
                    },
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
        target_env="staging",
        image_tag="sha-123",
        service_public=True,
        legifrance=True,
        embeddings=True,
        run_ingestion=True,
        run_embeddings=True,
        wipe_existing_chunks=True,
        embedding_source="all",
        embedding_only_column="",
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    assert scaleway_data_jobs.upsert_and_start_jobs(args) == 0

    assert started == [
        ["service-public", "medallion"],
        ["service-public", "ingest", "--wipe-existing-chunks"],
        ["embeddings", "service-public"],
        ["legifrance", "bulk-dump"],
        ["legifrance", "ingest"],
        ["embeddings", "legifrance"],
    ]


def test_upsert_and_start_jobs_rejects_wipe_existing_chunks_with_partial_backfill(
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
                        "key": "service-public-ingestion",
                        "domain": "service_public",
                        "image": "service-public-ingestion",
                        "description": "Ingestion",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_ingestion": True,
                        "env_groups": [],
                        "args": ["service-public", "ingest"],
                    },
                    {
                        "key": "embeddings-service-public",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Service-Public embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "service-public"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "project-id")
    monkeypatch.setattr(scaleway_data_jobs, "list_definitions", lambda project_id, region, *, secrets, dry_run: {})

    args = SimpleNamespace(
        config=str(config_path),
        target_env="staging",
        image_tag="sha-123",
        service_public=True,
        legifrance=False,
        embeddings=True,
        run_ingestion=True,
        run_embeddings=True,
        wipe_existing_chunks=True,
        embedding_source="service_public",
        embedding_only_column="embedding_m3",
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    with pytest.raises(RuntimeError, match="full Service-Public embeddings backfill"):
        scaleway_data_jobs.upsert_and_start_jobs(args)


def test_upsert_and_start_jobs_filters_embeddings_source_and_appends_only_column(
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
                        "key": "embeddings-service-public",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Service-Public embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "service-public", "--dsn-env", "SCW_POSTGRES_DSN"],
                    },
                    {
                        "key": "embeddings-legifrance",
                        "domain": "embeddings",
                        "image": "embeddings-job",
                        "description": "Legifrance embeddings",
                        "cpu_limit": 1000,
                        "memory_limit": 2048,
                        "local_storage_capacity": 1024,
                        "job_timeout": "3600s",
                        "requires_embeddings": True,
                        "env_groups": [],
                        "args": ["embeddings", "legifrance", "--dsn-env", "SCW_POSTGRES_DSN"],
                    },
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
        target_env="staging",
        image_tag="sha-123",
        service_public=False,
        legifrance=False,
        embeddings=True,
        run_ingestion=False,
        run_embeddings=True,
        embedding_source="service_public",
        embedding_only_column="embedding_m3",
        service_public_fiche_config="config/service_public_fiches.json",
        legifrance_article_ids_json="config/legifrance_article_cids.json",
        wait=False,
        dry_run=False,
    )

    assert scaleway_data_jobs.upsert_and_start_jobs(args) == 0

    assert started == [["embeddings", "service-public", "--dsn-env", "SCW_POSTGRES_DSN", "--only-column", "embedding_m3"]]
