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
    "pdf_sources": [
        {"image": "pdf-sources-pipeline", "dockerfile": "Dockerfile.pdf_sources_pipeline"},
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
        # Sélection explicite uniquement (tracer bullet #246): un dispatch
        # source=all ne doit ni exiger les secrets Grist/Albert ni écrire le
        # corpus MI. Le cron/prod arrive en Phase F (#250).
        "pdf_sources": source == "pdf_sources",
        "embeddings": source in {"all", "embeddings", "matte", "mi", "masa", "mso"},
    }


def classify_from_files(files: list[str]) -> dict[str, bool]:
    result = {"service_public": False, "legifrance": False, "pdf_sources": False, "embeddings": False}
    has_common = False

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
            has_common = True
            continue

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

        if startswith_any(
            path,
            (
                "packages/data-engineering/src/assistant_rh_data_engineering/pdf_ministry/",
                "packages/data-engineering/src/assistant_rh_data_engineering/mi/",
                "packages/data-engineering/src/assistant_rh_data_engineering/masa/",
                "packages/data-engineering/src/assistant_rh_data_engineering/matte/",
                "packages/data-engineering/src/assistant_rh_data_engineering/mso/",
                "packages/data-engineering/src/assistant_rh_data_engineering/jobs/pdf_sources_",
            ),
        ) or path in {
            "Dockerfile.pdf_sources_pipeline",
            "config/scaleway_serverless_job_pdf_sources_mi.json",
            "config/scaleway_serverless_job_pdf_sources_masa.json",
            "config/scaleway_serverless_job_pdf_sources_matte.json",
            "config/scaleway_serverless_job_pdf_sources_mso.json",
            "config/mi_embedding_tables.json",
            "config/masa_embedding_tables.json",
            "config/mso_embedding_tables.json",
        }:
            result["pdf_sources"] = True

        if (
            path == "Dockerfile.embeddings_job"
            or path == "packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py"
            # Les *_embedding_tables.json des ministères PDF appartiennent au
            # domaine pdf_sources: leur ajout ne doit pas déclencher les
            # backfills SP/Légifrance.
            or (
                contains_any(path, ("embedding_tables", "embeddings_job"))
                and path not in {"config/mi_embedding_tables.json", "config/masa_embedding_tables.json", "config/mso_embedding_tables.json"}
            )
        ):
            result["embeddings"] = True

    # A change to shared/common CI or config files only triggers a full all-sources
    # preview when nothing source-specific changed. When a specific source also
    # changed, scope the preview to that source instead of fanning out to everything.
    if has_common and not (result["service_public"] or result["legifrance"] or result["pdf_sources"] or result["embeddings"]):
        return {"service_public": True, "legifrance": True, "pdf_sources": True, "embeddings": True}

    return result


def infer_embedding_source(selected: dict[str, bool], requested_source: str = "") -> str:
    if requested_source in {"service_public", "legifrance", "matte", "mi", "masa", "mso"}:
        return requested_source
    if requested_source == "pdf_sources":
        # Le domaine pdf_sources couvre plusieurs ministères (MI, MASA):
        # dispatcher le backfill d'un ministère précis se fait via source=mi
        # ou source=masa. Le mapping historique vers MI est conservé pour ne
        # pas relancer TOUS les backfills via le fallback "all".
        return "mi"
    if selected["service_public"] and not selected["legifrance"]:
        return "service_public"
    if selected["legifrance"] and not selected["service_public"]:
        return "legifrance"
    return "all"


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
        run_embeddings = source in {"embeddings", "matte", "mi", "masa", "mso"} or os.getenv("INPUT_RUN_EMBEDDINGS", "").strip().lower() == "true"
        if run_embeddings:
            selected["embeddings"] = True
        files: list[str] = []
        requested_embedding_source = os.getenv("INPUT_EMBEDDING_SOURCE") or source
        embedding_source = infer_embedding_source(selected, requested_embedding_source)
    else:
        files = changed_files()
        selected = classify_from_files(files)
        embedding_source = infer_embedding_source(selected)
        run_embeddings = selected["service_public"] or selected["legifrance"] or selected["embeddings"]
        if run_embeddings:
            selected["embeddings"] = True

    matrix: list[dict[str, str]] = []
    for domain in ("service_public", "legifrance", "pdf_sources", "embeddings"):
        if selected[domain]:
            matrix.extend(IMAGE_MATRIX[domain])

    outputs = {
        "service_public": str(selected["service_public"]).lower(),
        "legifrance": str(selected["legifrance"]).lower(),
        "pdf_sources": str(selected["pdf_sources"]).lower(),
        "embeddings": str(selected["embeddings"]).lower(),
        "run_embeddings": str(run_embeddings).lower(),
        "embedding_source": embedding_source,
        "has_builds": str(bool(matrix)).lower(),
        "has_runs": str(selected["service_public"] or selected["legifrance"] or selected["pdf_sources"] or selected["embeddings"]).lower(),
        "matrix": json.dumps({"include": matrix or [{"image": "noop", "dockerfile": "Dockerfile.service_public_pipeline"}]}),
        "changed_files": json.dumps(files),
    }
    print(json.dumps(outputs, indent=2))
    write_outputs(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
