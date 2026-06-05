from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def redact(text: str, secrets: list[str]) -> str:
    output = text
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            output = output.replace(secret, "***")
    return output


def run_scw(args: list[str], *, secrets: list[str]) -> str:
    result = subprocess.run(["scw", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stdout = redact(result.stdout.strip(), secrets)
        stderr = redact(result.stderr.strip(), secrets)
        command = redact("scw " + " ".join(args), secrets)
        raise RuntimeError(
            "Scaleway CLI command failed:\n"
            f"command: {command}\n"
            f"stdout: {stdout or '<empty>'}\n"
            f"stderr: {stderr or '<empty>'}"
        )
    return result.stdout


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("containers", "namespaces", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def find_namespace(namespace_name: str, project_id: str, region: str, *, secrets: list[str]) -> dict[str, Any] | None:
    output = run_scw(
        [
            "container",
            "namespace",
            "list",
            f"project-id={project_id}",
            f"name={namespace_name}",
            f"region={region}",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    namespaces = extract_items(json.loads(output or "[]"))
    for namespace in namespaces:
        if str(namespace.get("name", "")).strip() == namespace_name:
            return namespace
    return None


def create_namespace(namespace_name: str, project_id: str, region: str, *, secrets: list[str]) -> dict[str, Any]:
    output = run_scw(
        [
            "container",
            "namespace",
            "create",
            f"name={namespace_name}",
            f"project-id={project_id}",
            f"region={region}",
            "-w",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    payload = json.loads(output or "{}")
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError(f"Unable to create namespace {namespace_name!r}: invalid Scaleway response")
    return payload


def find_container(container_name: str, namespace_id: str, region: str, *, secrets: list[str]) -> dict[str, Any] | None:
    output = run_scw(
        [
            "container",
            "container",
            "list",
            f"namespace-id={namespace_id}",
            f"name={container_name}",
            f"region={region}",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    containers = extract_items(json.loads(output or "[]"))
    for container in containers:
        if str(container.get("name", "")).strip() == container_name:
            return container
    return None


def get_container(container_id: str, region: str, *, secrets: list[str]) -> dict[str, Any]:
    output = run_scw(
        [
            "container",
            "container",
            "get",
            container_id,
            f"region={region}",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    payload = json.loads(output or "{}")
    if not isinstance(payload, dict) or str(payload.get("id", "")).strip() != container_id:
        raise RuntimeError(f"Unable to retrieve container {container_id!r}: invalid Scaleway response")
    return payload


def container_settings_args(
    *,
    image_uri: str,
    min_scale: int,
    max_scale: int,
    memory_limit_mb: int,
    cpu_limit_milli: int,
    timeout_seconds: int,
    port: int,
    protocol: str,
    privacy: str,
    health_path: str,
    environment: dict[str, str],
    secret_environment: dict[str, str],
) -> list[str]:
    args = [
        f"registry-image={image_uri}",
        f"min-scale={min_scale}",
        f"max-scale={max_scale}",
        f"memory-limit={memory_limit_mb}",
        f"cpu-limit={cpu_limit_milli}",
        f"timeout={timeout_seconds}s",
        f"privacy={privacy}",
        f"protocol={protocol}",
        f"port={port}",
        f"health-check.http.path={health_path}",
        "health-check.failure-threshold=30",
        "health-check.interval=10s",
        "http-option=enabled",
        "sandbox=v2",
    ]

    for key, value in sorted(environment.items()):
        args.append(f"environment-variables.{key}={value}")

    for index, (key, value) in enumerate(sorted(secret_environment.items())):
        args.append(f"secret-environment-variables.{index}.key={key}")
        args.append(f"secret-environment-variables.{index}.value={value}")

    return args


def create_container(
    *,
    namespace_id: str,
    container_name: str,
    region: str,
    settings_args: list[str],
    secrets: list[str],
) -> dict[str, Any]:
    output = run_scw(
        [
            "container",
            "container",
            "create",
            f"namespace-id={namespace_id}",
            f"name={container_name}",
            *settings_args,
            f"region={region}",
            "-w",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    payload = json.loads(output or "{}")
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError(f"Unable to create container {container_name!r}: invalid Scaleway response")
    return payload


def update_container(
    *,
    container_id: str,
    region: str,
    settings_args: list[str],
    secrets: list[str],
) -> dict[str, Any]:
    output = run_scw(
        [
            "container",
            "container",
            "update",
            container_id,
            *settings_args,
            f"region={region}",
            "-w",
            "-o",
            "json",
        ],
        secrets=secrets,
    )
    payload = json.loads(output or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unable to update container {container_id!r}: invalid Scaleway response")
    return payload


@dataclass
class DeployResult:
    target_env: str
    project_id: str
    region: str
    namespace_name: str
    namespace_id: str
    container_name: str
    container_id: str
    container_domain_name: str
    image_uri: str
    health_path: str
    action: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create/update and deploy Assistant RH Streamlit container on Scaleway.")
    parser.add_argument("--target-env", choices=("staging", "prod"), required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--namespace-name", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--cpu-limit", type=int, default=1000)
    parser.add_argument("--memory-limit", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--min-scale", type=int)
    parser.add_argument("--max-scale", type=int, default=1)
    parser.add_argument("--privacy", choices=("public", "private"), default="public")
    parser.add_argument("--protocol", choices=("http1", "h2c"), default="http1")
    parser.add_argument("--health-path", default="/_stcore/health")
    parser.add_argument("--output-json", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    project_id = args.project_id or env_required("SCW_DEFAULT_PROJECT_ID")
    region = args.region or env_optional("SCW_DEFAULT_REGION", "fr-par")
    min_scale = args.min_scale if args.min_scale is not None else (0 if args.target_env == "staging" else 1)

    required_runtime = [
        "SCW_POSTGRES_DSN",
        "ALBERT_API_KEY",
        "SCALEWAY_API_KEY",
        "COOKIES_PASSWORD",
        "ADMIN_PASSWORD",
    ]
    missing_runtime = [name for name in required_runtime if not env_optional(name)]
    if missing_runtime:
        raise RuntimeError(
            "Missing required runtime environment variable(s) for Streamlit container: " + ", ".join(missing_runtime)
        )

    default_app_env = "staging" if args.target_env == "staging" else "production"
    container_env = {
        "APP_ENV": env_optional("APP_ENV", default_app_env),
        "APP_DB_TARGET": env_optional("APP_DB_TARGET", "scaleway"),
        "APP_SCALEWAY_ENV": env_optional("APP_SCALEWAY_ENV", default_app_env),
        "ALBERT_BASE_URL": env_optional("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1"),
        "SCALEWAY_BASE_URL": env_optional("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1"),
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": env_optional("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false"),
    }
    container_env = {key: value for key, value in container_env.items() if value}

    container_secret_env = {
        "SCW_POSTGRES_DSN": env_required("SCW_POSTGRES_DSN"),
        "ALBERT_API_KEY": env_required("ALBERT_API_KEY"),
        "SCALEWAY_API_KEY": env_required("SCALEWAY_API_KEY"),
        "COOKIES_PASSWORD": env_required("COOKIES_PASSWORD"),
        "ADMIN_PASSWORD": env_required("ADMIN_PASSWORD"),
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
        min_scale=min_scale,
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
        print(f"Creating container {args.container_name!r} with image {args.image_uri}...")
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
        print(f"Updating container {args.container_name!r} ({container_id}) to image {args.image_uri}...")
        update_container(
            container_id=container_id,
            region=region,
            settings_args=settings_args,
            secrets=secret_values,
        )
        action = "updated"

    deployed = get_container(container_id, region, secrets=secret_values)
    domain_name = str(deployed.get("domain_name") or "").strip()

    result = DeployResult(
        target_env=args.target_env,
        project_id=project_id,
        region=region,
        namespace_name=args.namespace_name,
        namespace_id=namespace_id,
        container_name=args.container_name,
        container_id=container_id,
        container_domain_name=domain_name,
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
