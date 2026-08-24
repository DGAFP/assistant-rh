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
    "r2": [
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
        # source=<ministère> scope le domaine pdf_sources à SON job medallion
        # (pdf_sources_ministry) — à 4 ministères, relancer l'un ne doit pas
        # redémarrer les trois autres (suivi de la revue #266).
        "pdf_sources": source in {"pdf_sources", "mi", "masa", "matte", "mso"},
        "embeddings": source in {"all", "embeddings", "matte", "mi", "masa", "mso"},
        # R2 reste une dérivation Gold explicitement revue : jamais inclus
        # dans source=all ni dans les crons d'ingestion.
        "r2": source == "r2",
    }


def classify_from_files(files: list[str]) -> dict[str, bool]:
    result = {"service_public": False, "legifrance": False, "pdf_sources": False, "embeddings": False, "r2": False}
    has_common = False

    for path in files:
        common = (
            path
            in {
                ".github/workflows/data-engineering-preview-staging.yml",
                ".github/workflows/data-engineering-cron-delta.yml",
            }
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

        if path == "packages/data-engineering/src/assistant_rh_data_engineering/jobs/r2_article_summaries.py":
            result["r2"] = True

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
    if has_common and not (result["service_public"] or result["legifrance"] or result["pdf_sources"] or result["embeddings"] or result["r2"]):
        return {"service_public": True, "legifrance": True, "pdf_sources": True, "embeddings": True, "r2": False}

    return result


def build_run_matrix(
    selected: dict[str, bool],
    *,
    run_embeddings: bool,
    embedding_source: str,
    pdf_sources_ministry: str = "",
) -> list[dict[str, str | bool]]:
    """Build independent source chains for the workflow run matrix.

    Each source keeps its ordered medallion -> ingestion -> embeddings chain in
    one matrix cell. Different sources can therefore run independently with
    ``fail-fast: false``: a Service-Public failure no longer prevents the
    Legifrance chain from starting (and conversely).
    """

    entries: list[dict[str, str | bool]] = []
    entries_by_source: dict[str, dict[str, str | bool]] = {}

    def add_source(name: str, *, service_public: bool = False, legifrance: bool = False, pdf_sources: bool = False) -> None:
        entry: dict[str, str | bool] = {
            "name": name,
            "service_public": service_public,
            "legifrance": legifrance,
            "pdf_sources": pdf_sources,
            "pdf_sources_ministry": pdf_sources_ministry if pdf_sources else "",
            "embeddings": False,
            "r2": False,
            "embedding_source": "all",
        }
        entries.append(entry)
        entries_by_source[name] = entry

    if selected["service_public"]:
        add_source("service-public", service_public=True)
    if selected["legifrance"]:
        add_source("legifrance", legifrance=True)
    if selected["pdf_sources"]:
        pdf_name = f"pdf-sources-{pdf_sources_ministry}" if pdf_sources_ministry else "pdf-sources"
        add_source(pdf_name, pdf_sources=True)
    if selected.get("r2", False):
        entries.append(
            {
                "name": "legifrance-r2-summaries",
                "service_public": False,
                "legifrance": False,
                "pdf_sources": False,
                "pdf_sources_ministry": "",
                "embeddings": False,
                "r2": True,
                "embedding_source": "all",
            }
        )

    if selected["embeddings"] and run_embeddings:
        embedding_targets = ["service_public", "legifrance"] if embedding_source == "all" else [embedding_source]
        parent_by_target = {
            "service_public": "service-public",
            "legifrance": "legifrance",
            "mi": f"pdf-sources-{pdf_sources_ministry}" if pdf_sources_ministry == "mi" else "pdf-sources",
            "masa": f"pdf-sources-{pdf_sources_ministry}" if pdf_sources_ministry == "masa" else "pdf-sources",
            "matte": f"pdf-sources-{pdf_sources_ministry}" if pdf_sources_ministry == "matte" else "pdf-sources",
            "mso": f"pdf-sources-{pdf_sources_ministry}" if pdf_sources_ministry == "mso" else "pdf-sources",
        }
        for target in embedding_targets:
            parent = entries_by_source.get(parent_by_target.get(target, ""))
            if parent is not None:
                parent["embeddings"] = True
                parent["embedding_source"] = target
                continue
            entries.append(
                {
                    "name": f"embeddings-{target.replace('_', '-')}",
                    "service_public": False,
                    "legifrance": False,
                    "pdf_sources": False,
                    "pdf_sources_ministry": "",
                    "embeddings": True,
                    "r2": False,
                    "embedding_source": target,
                }
            )

    return entries


def infer_embedding_source(selected: dict[str, bool], requested_source: str = "") -> str:
    if requested_source in {"service_public", "legifrance", "matte", "mi", "masa", "mso"}:
        return requested_source
    if requested_source == "pdf_sources":
        # Le domaine pdf_sources couvre les 4 ministères (MI, MASA, MATTE,
        # MSO): le backfill d'un ministère précis se dispatch via
        # source=<ministère>. Le mapping historique vers MI est conservé pour
        # ne pas relancer TOUS les backfills via le fallback "all".
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


def resolve_mode(raw: str | None) -> str:
    """Socle #288 — axe plan/apply, étendu à generate pour R2 uniquement.

    Défaut ``apply`` (comportement historique) ; toute valeur inconnue retombe
    sur ``apply`` par sécurité.
    """
    mode = (raw or "apply").strip().lower()
    return mode if mode in {"plan", "generate", "apply"} else "apply"


def main() -> int:
    pdf_sources_ministry = ""
    mode = resolve_mode(os.getenv("INPUT_MODE"))
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        source = os.getenv("INPUT_SOURCE") or "all"
        selected = classify_from_source(source)
        if source in {"mi", "masa", "matte", "mso"}:
            pdf_sources_ministry = source
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

    if mode == "generate" and not selected["r2"]:
        raise SystemExit("mode=generate est réservé à source=r2 ; les autres sources utilisent plan/apply.")

    if selected.get("r2", False) and mode != "apply":
        # R2 sélectionné hors apply : la condition des workflows ouvre le job
        # de run dès que r2 est vrai, quel que soit le mode. Sans ce garde,
        # source=r2 + run_embeddings=true démarrerait les backfills embeddings
        # (mutation d'index) pendant un run censé être plan/generate seul.
        run_embeddings = False
        selected["embeddings"] = False

    matrix: list[dict[str, str]] = []
    for domain in ("service_public", "legifrance", "pdf_sources", "embeddings", "r2"):
        if selected[domain]:
            matrix.extend(IMAGE_MATRIX[domain])

    run_matrix = build_run_matrix(
        selected,
        run_embeddings=run_embeddings,
        embedding_source=embedding_source,
        pdf_sources_ministry=pdf_sources_ministry,
    )
    noop_run = {
        "name": "noop",
        "service_public": False,
        "legifrance": False,
        "pdf_sources": False,
        "pdf_sources_ministry": "",
        "embeddings": False,
        "r2": False,
        "embedding_source": "all",
    }

    outputs = {
        "service_public": str(selected["service_public"]).lower(),
        "legifrance": str(selected["legifrance"]).lower(),
        "pdf_sources": str(selected["pdf_sources"]).lower(),
        "embeddings": str(selected["embeddings"]).lower(),
        "r2": str(selected["r2"]).lower(),
        "run_embeddings": str(run_embeddings).lower(),
        "embedding_source": embedding_source,
        "pdf_sources_ministry": pdf_sources_ministry,
        "mode": mode,
        "has_builds": str(bool(matrix)).lower(),
        "has_runs": str(
            selected["service_public"] or selected["legifrance"] or selected["pdf_sources"] or selected["embeddings"] or selected["r2"]
        ).lower(),
        "matrix": json.dumps({"include": matrix or [{"image": "noop", "dockerfile": "Dockerfile.service_public_pipeline"}]}),
        "run_matrix": json.dumps({"include": run_matrix or [noop_run]}),
        "changed_files": json.dumps(files),
    }
    print(json.dumps(outputs, indent=2))
    write_outputs(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
