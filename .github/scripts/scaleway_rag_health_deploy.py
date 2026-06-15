from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from scaleway_streamlit_deploy import (
    DeployResult,
    container_settings_args,
    create_container,
    create_namespace,
    env_optional,
    env_required,
    find_container,
    find_namespace,
    get_container,
    update_container,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create/update and deploy Assistant RH RAG health monitoring container on Scaleway.")
    parser.add_argument("--target-env", choices=("staging", "prod"), required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--namespace-name", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--cpu-limit", type=int, default=250)
    parser.add_argument("--memory-limit", type=int, default=512)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--min-scale", type=int, default=1)
    parser.add_argument("--max-scale", type=int, default=1)
    parser.add_argument("--privacy", choices=("public", "private"), default="private")
    parser.add_argument("--protocol", choices=("http1", "h2c"), default="http1")
    parser.add_argument("--health-path", default="/healthz")
    parser.add_argument("--output-json", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    project_id = args.project_id or env_required("SCW_DEFAULT_PROJECT_ID")
    region = args.region or env_optional("SCW_DEFAULT_REGION", "fr-par")
    app_env = "staging" if args.target_env == "staging" else "production"
    env_label = "staging" if args.target_env == "staging" else "prod"

    required_runtime = [
        "RAG_HEALTH_POSTGRES_DSN",
        "COCKPIT_METRICS_PUSH_URL",
        "COCKPIT_TOKEN_SECRET_KEY",
    ]
    missing_runtime = [name for name in required_runtime if not env_optional(name)]
    if missing_runtime:
        raise RuntimeError("Missing required RAG health runtime variable(s): " + ", ".join(missing_runtime))

    container_env = {
        "APP_ENV": env_optional("APP_ENV", app_env),
        "APP_SCALEWAY_ENV": env_optional("APP_SCALEWAY_ENV", app_env),
        "RAG_HEALTH_ENV_LABEL": env_optional("RAG_HEALTH_ENV_LABEL", env_label),
        "RAG_HEALTH_EXPORTER_PORT": str(args.port),
        "DB_HEALTH_POLL_INTERVAL_SECONDS": env_optional("DB_HEALTH_POLL_INTERVAL_SECONDS", "300"),
        "COCKPIT_METRICS_PUSH_URL": env_required("COCKPIT_METRICS_PUSH_URL"),
    }

    container_secret_env = {
        "RAG_HEALTH_POSTGRES_DSN": env_required("RAG_HEALTH_POSTGRES_DSN"),
        "COCKPIT_TOKEN_SECRET_KEY": env_required("COCKPIT_TOKEN_SECRET_KEY"),
    }

    secret_values = [
        *container_secret_env.values(),
        env_optional("SCW_ACCESS_KEY"),
        env_optional("SCW_SECRET_KEY"),
    ]

    namespace = find_namespace(args.namespace_name, project_id, region, secrets=secret_values)
    if namespace is None:
        print(f"Creating namespace {args.namespace_name!r} in {region}...")
        namespace = create_namespace(args.namespace_name, project_id, region, secrets=secret_values)
    namespace_id = str(namespace["id"])

    settings_args = container_settings_args(
        image_uri=args.image_uri,
        min_scale=args.min_scale,
        max_scale=args.max_scale,
        memory_limit_mb=args.memory_limit,
        cpu_limit_milli=args.cpu_limit,
        timeout_seconds=args.timeout,
        port=args.port,
        protocol=args.protocol,
        privacy=args.privacy,
        health_path=args.health_path,
        environment=container_env,
        secret_environment=container_secret_env,
    )

    existing = find_container(args.container_name, namespace_id, region, secrets=secret_values)
    if existing is None:
        print(f"Creating RAG health container {args.container_name!r} with image {args.image_uri}...")
        created = create_container(
            namespace_id=namespace_id,
            container_name=args.container_name,
            region=region,
            settings_args=settings_args,
            secrets=secret_values,
        )
        container_id = str(created["id"])
        action = "created"
    else:
        container_id = str(existing["id"])
        print(f"Updating RAG health container {args.container_name!r} ({container_id}) to image {args.image_uri}...")
        update_container(
            container_id=container_id,
            region=region,
            settings_args=settings_args,
            secrets=secret_values,
        )
        action = "updated"

    deployed = get_container(container_id, region, secrets=secret_values)
    result = DeployResult(
        target_env=args.target_env,
        project_id=project_id,
        region=region,
        namespace_name=args.namespace_name,
        namespace_id=namespace_id,
        container_name=args.container_name,
        container_id=container_id,
        container_domain_name=str(deployed.get("domain_name") or "").strip(),
        image_uri=args.image_uri,
        health_path=args.health_path,
        action=action,
    )

    payload = asdict(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
