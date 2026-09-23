"""Provision a native Scaleway schedule using configuration supplied at runtime.

Credentials are injected through Secret Manager references. Never print API
responses: an existing job may still contain legacy plaintext credentials.
"""

from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

import requests
from assistant_rh_rag_pipeline.feedback_grist import FeedbackGristDestination, feedback_source_environment
from assistant_rh_rag_pipeline.feedback_grist_sync import parse_since

SECRET_NAMES = ("SCW_POSTGRES_DSN", "GRIST_API_KEY")


def job_secrets() -> list[dict]:
    """Only Secret Manager IDs/versions are needed by the deployment process."""
    return [
        {
            "env_var_name": name,
            "secret_manager_id": str(UUID(os.environ[f"{name}_SECRET_ID"].strip())),
            "secret_manager_version": os.getenv(f"{name}_SECRET_VERSION", "latest").strip() or "latest",
        }
        for name in SECRET_NAMES
    ]


def job_settings(image: str, since: str, schedule: str | None) -> dict:
    config = FeedbackGristDestination.from_env()
    environment = feedback_source_environment()
    settings = {
        "name": f"assistant-rh-feedback-grist-{environment}",
        "description": "Feedbacks vers Grist ; annotations humaines conservées ; configuration gérée dans Scaleway.",
        "image_uri": image,
        "cpu_limit": 1000,
        "memory_limit": 512,
        "local_storage_capacity": 1000,
        "job_timeout": "600s",
        "retry_policy": {"max_retries": 1},
        "startup_command": ["python", "-m", "assistant_rh_rag_pipeline.feedback_grist_sync"],
        "args": [],
        "environment_variables": {
            "APP_ENV": environment,
            "APP_SCALEWAY_ENV": environment,
            "APP_DB_TARGET": "scaleway",
            "GRIST_API_BASE_URL": config.base_url,
            "GRIST_FEEDBACK_DOC_ID": config.doc_id,
            "GRIST_FEEDBACK_TABLE_ID": config.table_id,
            "GRIST_FEEDBACK_SINCE": parse_since(since).isoformat(),
        },
    }
    if schedule:
        settings["cron_schedule"] = {"schedule": schedule, "timezone": "Europe/Paris"}
    return settings


def deploy(settings: dict, secrets: list[dict], *, start: bool = False) -> dict:
    project = os.environ["SCW_DEFAULT_PROJECT_ID"]
    region = os.getenv("SCW_DEFAULT_REGION", "fr-par")
    base = f"https://api.scaleway.com/serverless-jobs/v1alpha2/regions/{region}"
    session = requests.Session()
    session.headers["X-Auth-Token"] = os.environ["SCW_SECRET_KEY"]

    def request(method: str, path: str, **kwargs) -> dict:
        try:
            response = session.request(method, base + path, timeout=(5, 30), allow_redirects=False, **kwargs)
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"Scaleway : HTTP {response.status_code}")
            return response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("Scaleway : erreur réseau ou réponse invalide.") from None

    matches = []
    page = 1
    while True:
        definitions = request("GET", "/job-definitions", params={"project_id": project, "page_size": 100, "page": page})["job_definitions"]
        matches.extend(job for job in definitions if job["name"] == settings["name"])
        if len(definitions) < 100:
            break
        page += 1
    if len(matches) > 1:
        raise RuntimeError("Plusieurs jobs portent ce nom ; aucun job modifié.")
    if matches:
        result = matches[0]
    else:
        # A new job must not run on its cron before both secrets are attached.
        initial = {k: v for k, v in settings.items() if k != "cron_schedule"}
        result = request("POST", "/job-definitions", json={**initial, "project_id": project})
    job_id = result["id"]
    references = request("GET", "/secrets", params={"job_definition_id": job_id})["secrets"]
    existing = {}
    for desired in secrets:
        name = desired["env_var_name"]
        found = [ref for ref in references if (ref.get("env_var") or {}).get("name") == name]
        if len(found) > 1 or (found and found[0]["secret_manager_id"] != desired["secret_manager_id"]):
            raise RuntimeError(f"Référence Secret Manager ambiguë ou différente pour {name} ; vérifiez le job avant de relancer.")
        if found:
            existing[name] = found[0]
    for desired in secrets:
        current = existing.get(desired["env_var_name"])
        if current is None:
            request("POST", "/secrets", json={"job_definition_id": job_id, "secrets": [desired]})
        elif current["secret_manager_version"] != desired["secret_manager_version"]:
            request("PATCH", "/secrets/" + current["secret_id"], json={"secret_manager_version": desired["secret_manager_version"]})
    # Replace ordinary variables only once the required references exist. This
    # also removes credentials from definitions created by the old script.
    result = request("PATCH", "/job-definitions/" + job_id, json=settings)
    if any(name in result["environment_variables"] for name in SECRET_NAMES):
        raise RuntimeError("Des identifiants restent dans les variables ordinaires du job ; déploiement non confirmé.")
    report = {"job_id": result["id"], "name": settings["name"], "image": settings["image_uri"]}
    if start:
        run = request("POST", "/job-definitions/" + result["id"] + "/start", json={})
        report["run_ids"] = [item["id"] for item in run["job_runs"]]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Créer/actualiser le job Grist Scaleway sans modifier le conteneur Streamlit.")
    parser.add_argument("--image", required=True, help="Image du job déjà publiée dans le registry Scaleway.")
    parser.add_argument("--since", required=True, help="Date ISO inclusive du périmètre de revue.")
    parser.add_argument("--schedule", help="Cron natif Scaleway ; omis : pas de planification créée/modifiée.")
    parser.add_argument("--start", action="store_true", help="Lancer une exécution immédiate.")
    parser.add_argument("--dry-run", action="store_true", help="Afficher le plan sans secrets ni appel Scaleway.")
    args = parser.parse_args()
    try:
        settings = job_settings(args.image, args.since, args.schedule)
        secrets = job_secrets()
        if args.dry_run:
            report = {k: v for k, v in settings.items() if k != "environment_variables"}
            report["environment_variable_names"] = sorted(settings["environment_variables"])
            report["secret_references"] = secrets
        else:
            report = deploy(settings, secrets, start=args.start)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not expose request bodies, environment values or raw exceptions.
        print(json.dumps({"status": "error", "error": f"Déploiement non confirmé ({type(exc).__name__})."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
