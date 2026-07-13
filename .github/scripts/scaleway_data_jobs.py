from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "data-engineering-jobs.json"

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off", ""}


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in TRUTHY:
        return True
    if normalized in FALSY:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create/update Scaleway Serverless Jobs and start the selected data engineering jobs.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--target-env", choices=("staging", "prod"), required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--service-public", type=parse_bool, default=False)
    parser.add_argument("--legifrance", type=parse_bool, default=False)
    parser.add_argument("--pdf-sources", type=parse_bool, default=False)
    parser.add_argument("--embeddings", type=parse_bool, default=False)
    parser.add_argument("--run-ingestion", type=parse_bool, default=False)
    parser.add_argument("--run-embeddings", type=parse_bool, default=False)
    parser.add_argument("--service-public-fiche-config", default="config/service_public_fiches.json")
    parser.add_argument("--wipe-existing-chunks", type=parse_bool, default=False)
    parser.add_argument("--legifrance-article-ids-json", default="config/legifrance_article_cids.json")
    parser.add_argument("--embedding-source", choices=("all", "service_public", "legifrance", "matte", "mi", "masa", "mso"), default="all")
    parser.add_argument("--pdf-sources-ministry", choices=("", "mi", "masa", "matte", "mso"), default="")
    parser.add_argument("--embedding-only-column", default="")
    # Socle #288 — axe plan/apply. `plan` = détection seule, aucune mutation
    # (ni ingestion Postgres, ni backfill embeddings) ; `apply` (défaut) =
    # comportement historique. Le gating prod (apply prod = confirmation
    # explicite) est câblé avec le workflow en PR-3.
    parser.add_argument("--mode", choices=("plan", "apply"), default="apply")
    # Chaîne delta quotidienne (#250) : ajoute --delta aux jobs medallion+ingest
    # SP/Legi (et --from-grist au medallion SP) pour ne (re)traiter que le
    # new/changed. Le chemin legacy (upsert-all) reste le défaut.
    parser.add_argument("--delta", type=parse_bool, default=False)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def redacted(text: str, secrets: list[str]) -> str:
    output = text
    # Longest first: redacting a secret that contains another one must not leave
    # the remainder of the longer secret in clear text.
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        output = output.replace(secret, "***")
    return output


def run_scw(args: list[str], *, secrets: list[str], dry_run: bool = False) -> str:
    printable = " ".join(redacted(part, secrets) for part in args)
    if dry_run:
        print(f"[dry-run] scw {printable}")
        return "{}"

    result = subprocess.run(
        ["scw", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stdout = redacted(result.stdout.strip(), secrets)
        stderr = redacted(result.stderr.strip(), secrets)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr)
        raise RuntimeError(f"Scaleway CLI command failed: scw {printable}")
    return result.stdout


def extract_definitions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "job_definitions", "definitions", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def list_definitions(project_id: str, region: str, *, secrets: list[str], dry_run: bool) -> dict[str, dict[str, Any]]:
    if dry_run:
        return {}
    output = run_scw(
        [
            "jobs",
            "definition",
            "list",
            f"project-id={project_id}",
            f"region={region}",
            "-o",
            "json",
        ],
        secrets=secrets,
        dry_run=False,
    )
    payload = json.loads(output or "[]")
    definitions = extract_definitions(payload)
    return {str(item.get("name") or ""): item for item in definitions if item.get("name")}


def definition_id(definition: dict[str, Any]) -> str:
    job_id = str(definition.get("id") or definition.get("ID") or "").strip()
    if not job_id:
        raise RuntimeError(f"Unable to find Scaleway job definition id for {definition.get('name')!r}")
    return job_id


def extract_run(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("run", "job_run", "job"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def render_args(values: list[str], context: dict[str, str]) -> list[str]:
    return [value.format(**context) for value in values]


# Jobs medallion+ingest SP/Legi éligibles au delta (les autres — bulk-dump,
# embeddings, PDF — gardent leur comportement legacy).
_DELTA_JOB_KEYS = frozenset({"service-public-medallion", "service-public-ingestion", "legifrance-medallion", "legifrance-ingestion"})


def delta_args_for(key: str) -> list[str]:
    """Flags à ajouter à un job en chaîne delta (#250).

    ``--delta`` pour les medallion+ingest SP/Legi ; ``--from-grist`` en plus pour
    le medallion SP (piloté par le référentiel Grist — le medallion Legi reste
    cache-driven via ``article_cids``).
    """
    extra: list[str] = []
    if key in _DELTA_JOB_KEYS:
        extra.append("--delta")
    if key == "service-public-medallion":
        extra.append("--from-grist")
    return extra


def delta_env_groups_for(key: str) -> list[str]:
    """Env groups SUPPLÉMENTAIRES requis par un job en mode delta (#250).

    Ajoutés UNIQUEMENT quand --delta est actif, pour ne pas casser le chemin
    legacy (dont les workflows ne fournissent pas GRIST_*/LEGIFRANCE_*) :
    - medallion SP (--from-grist) et ingest SP (--delta) lisent le référentiel
      Grist -> ``grist`` ;
    - ingest Legi (--delta) lit Grist ET la TOC PISTE (follow-live) -> ``grist`` + ``piste`` ;
    - medallion Legi (--delta) reste cache-driven (article_cids) -> aucun ajout.
    """
    if key in {"service-public-medallion", "service-public-ingestion"}:
        return ["grist"]
    if key == "legifrance-ingestion":
        return ["grist", "piste"]
    return []


def indexed_args(prefix: str, values: list[str]) -> list[str]:
    return [f"{prefix}.{index}={value}" for index, value in enumerate(values)]


def image_uri(region: str, namespace: str, image: str, image_tag: str) -> str:
    return f"rg.{region}.scw.cloud/{namespace}/{image}:{image_tag}"


def create_definition(
    spec: dict[str, Any],
    name: str,
    image: str,
    project_id: str,
    region: str,
    *,
    secrets: list[str],
    dry_run: bool,
) -> str:
    args = [
        "jobs",
        "definition",
        "create",
        f"name={name}",
        f"description={spec['description']}",
        f"cpu-limit={spec['cpu_limit']}",
        f"memory-limit={spec['memory_limit']}",
        f"local-storage-capacity={spec['local_storage_capacity']}",
        f"image-uri={image}",
        "startup-command.0=data-ingestion",
        f"job-timeout={spec['job_timeout']}",
        f"project-id={project_id}",
        f"region={region}",
        "-o",
        "json",
    ]
    output = run_scw(args, secrets=secrets, dry_run=dry_run)
    if dry_run:
        return "dry-run"
    payload = json.loads(output or "{}")
    if isinstance(payload, dict) and "id" in payload:
        return definition_id(payload)
    definitions = extract_definitions(payload)
    if definitions:
        return definition_id(definitions[0])
    raise RuntimeError(f"Scaleway did not return the created definition id for {name}.")


def update_definition(
    spec: dict[str, Any],
    job_id: str,
    image: str,
    region: str,
    *,
    secrets: list[str],
    dry_run: bool,
) -> None:
    run_scw(
        [
            "jobs",
            "definition",
            "update",
            job_id,
            f"description={spec['description']}",
            f"cpu-limit={spec['cpu_limit']}",
            f"memory-limit={spec['memory_limit']}",
            f"local-storage-capacity={spec['local_storage_capacity']}",
            f"image-uri={image}",
            "startup-command.0=data-ingestion",
            f"job-timeout={spec['job_timeout']}",
            f"region={region}",
        ],
        secrets=secrets,
        dry_run=dry_run,
    )


def base_environment(target_env: str, region: str) -> dict[str, str]:
    return {
        "SCW_DEFAULT_REGION": region,
        "SCW_BUCKET_BRONZE": env_optional("SCW_BUCKET_BRONZE", "assistant-rh-bronze"),
        "SCW_BUCKET_SILVER": env_optional("SCW_BUCKET_SILVER", "assistant-rh-silver"),
        "SCW_BUCKET_GOLD": env_optional("SCW_BUCKET_GOLD", "assistant-rh-gold"),
        "SCW_PREFIX_STAGING": env_optional("SCW_PREFIX_STAGING", "staging"),
        "SCW_PREFIX_PROD": env_optional("SCW_PREFIX_PROD", "prod"),
        "TARGET_ENV": target_env,
    }


def job_environment(spec: dict[str, Any], target_env: str, region: str) -> dict[str, str]:
    env = base_environment(target_env, region)
    groups = set(spec.get("env_groups") or [])
    if "object_storage" in groups:
        env["SCW_ACCESS_KEY"] = env_required("SCW_ACCESS_KEY")
        env["SCW_SECRET_KEY"] = env_required("SCW_SECRET_KEY")
        env["SCW_BUCKET_SOURCES_PDF"] = env_optional("SCW_BUCKET_SOURCES_PDF", "assistant-rh-sources-pdf")
    if "postgres" in groups:
        env["SCW_POSTGRES_DSN"] = env_required("SCW_POSTGRES_DSN")
    if "embeddings_api" in groups:
        env["SCALEWAY_API_KEY"] = env_required("SCALEWAY_API_KEY")
        env["SCALEWAY_BASE_URL"] = env_optional("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1")
    if "grist" in groups:
        env["GRIST_API_BASE_URL"] = env_required("GRIST_API_BASE_URL")
        env["GRIST_API_KEY"] = env_required("GRIST_API_KEY")
        env["GRIST_DOC_ID"] = env_required("GRIST_DOC_ID")
        grist_table = env_optional("GRIST_TABLE_ID")
        if grist_table:
            env["GRIST_TABLE_ID"] = grist_table
    if "albert" in groups:
        env["ALBERT_API_KEY"] = env_required("ALBERT_API_KEY")
        env["ALBERT_BASE_URL"] = env_optional("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1")
    if "piste" in groups:
        # API Légifrance (follow-live des textes suivis, mode --delta Legi).
        # Groupe préparé mais PAS encore activé sur les jobs legifrance : le
        # basculement CI du delta (avec pose des secrets LEGIFRANCE_*) est
        # l'étape cron #250 — le chemin legacy upsert-all reste inchangé.
        env["LEGIFRANCE_CLIENT_ID"] = env_required("LEGIFRANCE_CLIENT_ID")
        env["LEGIFRANCE_CLIENT_SECRET"] = env_required("LEGIFRANCE_CLIENT_SECRET")
    return env


def plan_mode_overrides(mode: str, run_ingestion: bool, run_embeddings: bool) -> tuple[bool, bool]:
    """En mode ``plan`` on ne mute rien : ni ingestion Postgres, ni backfill
    embeddings. Le socle #288 sépare la détection (``plan``) de la
    réconciliation (``apply``) ; ``apply`` conserve le comportement historique.

    Renvoie ``(run_ingestion, run_embeddings)`` effectifs.
    """
    if mode == "plan":
        return False, False
    return run_ingestion, run_embeddings


def should_run(spec: dict[str, Any], args: argparse.Namespace) -> bool:
    domain = spec["domain"]
    if getattr(args, "delta", False) and str(spec.get("key") or "") == "legifrance-bulk-dump":
        # Chaîne delta quotidienne (#250) = medallion->ingest->embeddings. Le bulk
        # dump (raw DILA, job legacy ~4h) refresh le contenu à une cadence séparée,
        # il n'est jamais démarré par le cron delta.
        return False
    if domain == "service_public" and not args.service_public:
        return False
    if domain == "legifrance" and not args.legifrance:
        return False
    if domain == "pdf_sources" and not getattr(args, "pdf_sources", False):
        return False
    if domain == "pdf_sources":
        # Granularité par ministère (revue #266/#267): source=<ministère> ne
        # démarre que SON job medallion — pas les 3 autres corpus.
        ministry = str(getattr(args, "pdf_sources_ministry", "") or "")
        if ministry and str(spec.get("key") or "") != f"pdf-sources-{ministry}-medallion":
            return False
    if domain == "embeddings" and not args.embeddings:
        return False
    if os.getenv("GITHUB_EVENT_NAME") == "push" and spec.get("auto_start_on_push") is False:
        return False
    if domain == "embeddings":
        embedding_source = getattr(args, "embedding_source", "all") or "all"
        key = str(spec.get("key") or "")
        if embedding_source != "all" and key != f"embeddings-{embedding_source.replace('_', '-')}":
            return False
        # Backfills à sélection explicite (ex: embeddings-mi, dont la table
        # n'existe pas encore partout): jamais démarrés par embedding_source=all.
        if embedding_source == "all" and spec.get("requires_explicit_embedding_source"):
            return False
    if spec.get("requires_ingestion") and not args.run_ingestion:
        return False
    if spec.get("requires_embeddings") and not args.run_embeddings:
        return False
    return True


def validate_wipe_existing_chunks_selection(selected_specs: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if not getattr(args, "wipe_existing_chunks", False):
        return

    keys = {str(spec.get("key") or "") for spec in selected_specs}
    if "service-public-ingestion" not in keys:
        return
    if str(getattr(args, "embedding_only_column", "") or "").strip():
        raise RuntimeError("--wipe-existing-chunks requires a full Service-Public embeddings backfill: do not set --embedding-only-column.")
    if "embeddings-service-public" in keys:
        return

    raise RuntimeError(
        "--wipe-existing-chunks requires selecting the Service-Public embeddings backfill too: "
        "set run_embeddings=true and embedding_source=service_public or all."
    )


def order_specs_for_wipe_existing_chunks(selected_specs: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not getattr(args, "wipe_existing_chunks", False):
        return selected_specs

    backfill_spec = next((spec for spec in selected_specs if spec.get("key") == "embeddings-service-public"), None)
    has_ingestion = any(spec.get("key") == "service-public-ingestion" for spec in selected_specs)
    if backfill_spec is None or not has_ingestion:
        return selected_specs

    ordered_specs: list[dict[str, Any]] = []
    for spec in selected_specs:
        key = spec.get("key")
        if key == "embeddings-service-public":
            continue
        ordered_specs.append(spec)
        if key == "service-public-ingestion":
            ordered_specs.append(backfill_spec)
    return ordered_specs


def start_definition(
    job_id: str,
    spec: dict[str, Any],
    command_args: list[str],
    environment: dict[str, str],
    region: str,
    *,
    wait: bool,
    secrets: list[str],
    dry_run: bool,
) -> None:
    args = [
        "jobs",
        "definition",
        "start",
        job_id,
        "startup-command.0=data-ingestion",
        *indexed_args("args", command_args),
        *[f"environment-variables.{key}={value}" for key, value in sorted(environment.items())],
        f"region={region}",
        "-o",
        "json",
    ]
    if wait:
        args.append("-w")
    output = run_scw(args, secrets=secrets, dry_run=dry_run)
    if dry_run or not wait:
        return

    try:
        payload = json.loads(output or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unable to parse Scaleway run output for {spec.get('key')}: {redacted(output, secrets)}") from exc

    run = extract_run(payload)
    state = str(run.get("state") or "").strip().lower()
    if state != "succeeded":
        run_id = str(run.get("id") or "").strip()
        error_message = str(run.get("error_message") or "").strip()
        reason = str(run.get("reason") or "").strip()
        details = ", ".join(
            part
            for part in (
                f"id={run_id}" if run_id else "",
                f"state={state}" if state else "",
                f"reason={reason}" if reason else "",
            )
            if part
        )
        if error_message:
            details = f"{details}: {redacted(error_message, secrets)}" if details else redacted(error_message, secrets)
        raise RuntimeError(f"Scaleway job run failed for {spec.get('key')}: {details}")


def upsert_and_start_jobs(args: argparse.Namespace) -> int:
    # Socle #288 : en mode plan, neutraliser toute mutation avant la sélection
    # des jobs (les specs requires_ingestion / requires_embeddings ne seront
    # alors pas retenues par should_run).
    args.run_ingestion, args.run_embeddings = plan_mode_overrides(
        getattr(args, "mode", "apply"),
        getattr(args, "run_ingestion", False),
        getattr(args, "run_embeddings", False),
    )
    config = load_config(Path(args.config))
    region = env_optional("SCW_DEFAULT_REGION", "fr-par")
    project_id = env_required("SCW_DEFAULT_PROJECT_ID")
    namespace = env_optional("SCW_CONTAINER_REGISTRY_NAMESPACE", "assistant-rh")
    secrets = [
        env_optional("SCW_ACCESS_KEY"),
        env_optional("SCW_SECRET_KEY"),
        env_optional("SCW_POSTGRES_DSN"),
        env_optional("SCALEWAY_API_KEY"),
        env_optional("GRIST_API_KEY"),
        env_optional("ALBERT_API_KEY"),
        env_optional("LEGIFRANCE_CLIENT_SECRET"),
    ]
    context = {
        "target_env": args.target_env,
        "service_public_fiche_config": args.service_public_fiche_config,
        "legifrance_article_ids_json": args.legifrance_article_ids_json,
    }
    definitions = list_definitions(project_id, region, secrets=secrets, dry_run=args.dry_run)
    selected_specs = [spec for spec in config["jobs"] if should_run(spec, args)]
    validate_wipe_existing_chunks_selection(selected_specs, args)
    selected_specs = order_specs_for_wipe_existing_chunks(selected_specs, args)

    if not selected_specs:
        print("No Scaleway data engineering job selected.")
        return 0

    for spec in selected_specs:
        name = config["job_name_template"].format(target_env=args.target_env, key=spec["key"])
        image = image_uri(region, namespace, spec["image"], args.image_tag)
        existing = definitions.get(name)
        if existing:
            job_id = definition_id(existing)
            print(f"Updating Scaleway job {name} -> {image}")
            update_definition(spec, job_id, image, region, secrets=secrets, dry_run=args.dry_run)
        else:
            print(f"Creating Scaleway job {name} -> {image}")
            job_id = create_definition(spec, name, image, project_id, region, secrets=secrets, dry_run=args.dry_run)

        command_args = render_args(spec["args"], context)
        if getattr(args, "delta", False):
            command_args.extend(delta_args_for(str(spec.get("key") or "")))
        if spec.get("key") == "service-public-ingestion" and getattr(args, "wipe_existing_chunks", False):
            command_args.append("--wipe-existing-chunks")
        embedding_only_column = str(getattr(args, "embedding_only_column", "") or "").strip()
        if spec["domain"] == "embeddings" and embedding_only_column:
            command_args.extend(["--only-column", embedding_only_column])
        if args.target_env == "prod":
            command_args.extend(render_args(spec.get("prod_args") or [], context))
        environment_spec = spec
        if getattr(args, "delta", False):
            extra_groups = delta_env_groups_for(str(spec.get("key") or ""))
            if extra_groups:
                environment_spec = {**spec, "env_groups": list(spec.get("env_groups") or []) + extra_groups}
        environment = job_environment(environment_spec, args.target_env, region)
        print(f"Starting Scaleway job {name}: data-ingestion {' '.join(command_args)}")
        start_definition(
            job_id,
            spec,
            command_args,
            environment,
            region,
            wait=args.wait,
            secrets=secrets,
            dry_run=args.dry_run,
        )

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return upsert_and_start_jobs(args)


if __name__ == "__main__":
    raise SystemExit(main())
