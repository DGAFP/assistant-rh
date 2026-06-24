from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

IMAGE_MATRIX = {
    "service_public": [
        {"image": "service-public-pipeline", "dockerfile": "Dockerfile.service_public_pipeline"},
        {"image": "service-public-ingestion", "dockerfile": "Dockerfile.service_public_ingestion"},
    ],
    "legifrance": [
        {"image": "legifrance-bulk-dump", "dockerfile": "Dockerfile.legifrance_bulk_dump"},
        {"image": "legifrance-pipeline", "dockerfile": "Dockerfile.legifrance_pipeline"},
        {"image": "legifrance-ingestion", "dockerfile": "Dockerfile.legifrance_ingestion"},
    ],
    "embeddings": [
        {"image": "embeddings-job", "dockerfile": "Dockerfile.embeddings_job"},
    ],
}


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def changed_files() -> list[str]:
    before = os.getenv("GITHUB_EVENT_BEFORE") or os.getenv("BEFORE") or ""
    sha = os.getenv("GITHUB_SHA") or "HEAD"
    if before and before != "0" * 40:
        base = before
    else:
        try:
            base = git_output("rev-parse", f"{sha}^")
        except subprocess.CalledProcessError:
            return [path for path in git_output("ls-files").splitlines() if path]

    try:
        return [path for path in git_output("diff", "--name-only", base, sha).splitlines() if path]
    except subprocess.CalledProcessError:
        return [path for path in git_output("ls-files").splitlines() if path]


def startswith_any(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def contains_any(path: str, needles: tuple[str, ...]) -> bool:
    return any(needle in path for needle in needles)


def classify_from_source(source: str) -> dict[str, bool]:
    source = source or "all"
    return {
        "service_public": source in {"all", "service_public"},
        "legifrance": source in {"all", "legifrance"},
        "embeddings": source in {"all", "embeddings", "matte"},
    }


def classify_from_files(files: list[str]) -> dict[str, bool]:
    result = {"service_public": False, "legifrance": False, "embeddings": False}

    for path in files:
        common = (
            path.startswith(".github/workflows/data-engineering-")
            or path == ".github/scripts/data_engineering_plan.py"
            or path == ".github/scripts/scaleway_data_jobs.py"
            or path == ".github/data-engineering-jobs.json"
            or path == "uv.lock"
            or path.startswith("apps/data-ingestion-cli/")
            or path == "packages/data-engineering/pyproject.toml"
            or path == "packages/data-engineering/src/assistant_rh_data_engineering/cli.py"
            or path == "packages/data-engineering/src/assistant_rh_data_engineering/jobs/__init__.py"
            or startswith_any(
                path,
                (
                    "packages/shared-config/",
                    "packages/data-engineering/src/assistant_rh_data_engineering/utils/",
                ),
            )
        )
        if common:
            return {"service_public": True, "legifrance": True, "embeddings": True}

        if startswith_any(
            path,
            (
                "packages/data-engineering/src/assistant_rh_data_engineering/service_public/",
                "packages/data-engineering/src/assistant_rh_data_engineering/jobs/service_public_",
            ),
        ) or path in {
            "Dockerfile.service_public_pipeline",
            "Dockerfile.service_public_ingestion",
            "config/service_public_fiches.json",
            "config/scaleway_serverless_job_service_public.json",
            "config/scaleway_serverless_job_service_public_ingestion.json",
        }:
            result["service_public"] = True

        if startswith_any(
            path,
            (
                "packages/data-engineering/src/assistant_rh_data_engineering/legifrance/",
                "packages/data-engineering/src/assistant_rh_data_engineering/jobs/legifrance_",
            ),
        ) or path in {
            "Dockerfile.legifrance_bulk_dump",
            "Dockerfile.legifrance_pipeline",
            "Dockerfile.legifrance_ingestion",
            "config/legifrance_article_cids.json",
            "config/legifrance_articles.json",
            "config/legifrance_articles_smoke.json",
            "config/legifrance_legacy_texts.json",
            "config/scaleway_serverless_job_legifrance.json",
            "config/scaleway_serverless_job_legifrance_ingestion.json",
        }:
            result["legifrance"] = True

        if (
            path == "Dockerfile.embeddings_job"
            or path == "packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py"
            or contains_any(path, ("embedding_tables", "embeddings_job"))
        ):
            result["embeddings"] = True

    return result


def write_outputs(outputs: dict[str, str]) -> None:
    github_output = os.getenv("GITHUB_OUTPUT")
    if not github_output:
        return
    output_path = Path(github_output)
    with output_path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        source = os.getenv("INPUT_SOURCE") or "all"
        selected = classify_from_source(source)
        run_embeddings = source in {"embeddings", "matte"} or os.getenv("INPUT_RUN_EMBEDDINGS", "").strip().lower() == "true"
        if run_embeddings:
            selected["embeddings"] = True
        files: list[str] = []
    else:
        files = changed_files()
        selected = classify_from_files(files)
        run_embeddings = selected["embeddings"]

    matrix: list[dict[str, str]] = []
    for domain in ("service_public", "legifrance", "embeddings"):
        if selected[domain]:
            matrix.extend(IMAGE_MATRIX[domain])

    outputs = {
        "service_public": str(selected["service_public"]).lower(),
        "legifrance": str(selected["legifrance"]).lower(),
        "embeddings": str(selected["embeddings"]).lower(),
        "run_embeddings": str(run_embeddings).lower(),
        "has_builds": str(bool(matrix)).lower(),
        "has_runs": str(selected["service_public"] or selected["legifrance"] or selected["embeddings"]).lower(),
        "matrix": json.dumps({"include": matrix or [{"image": "noop", "dockerfile": "Dockerfile.service_public_pipeline"}]}),
        "changed_files": json.dumps(files),
    }
    print(json.dumps(outputs, indent=2))
    write_outputs(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
