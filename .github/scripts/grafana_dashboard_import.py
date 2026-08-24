from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def build_payload(
    dashboard: dict[str, Any],
    *,
    folder_uid: str = "",
    overwrite: bool = True,
    message: str = "",
) -> dict[str, Any]:
    if not dashboard.get("uid"):
        raise ValueError("Grafana dashboard must define a stable uid before it can be provisioned")

    payload: dict[str, Any] = {
        "dashboard": dashboard,
        "overwrite": overwrite,
    }
    if folder_uid:
        payload["folderUid"] = folder_uid
    if message:
        payload["message"] = message
    return payload


def auth_headers(*, api_token: str = "", basic_auth: str = "") -> dict[str, str]:
    if api_token:
        return {"Authorization": f"Bearer {api_token}"}
    if basic_auth:
        encoded = base64.b64encode(basic_auth.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    raise RuntimeError("Missing Grafana authentication: set GRAFANA_API_TOKEN or GRAFANA_BASIC_AUTH")


def import_dashboard(
    *,
    grafana_url: str,
    api_token: str = "",
    basic_auth: str = "",
    payload: dict[str, Any],
    timeout: int = 30,
) -> dict[str, Any]:
    url = grafana_url.rstrip("/") + "/api/dashboards/db"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            **auth_headers(api_token=api_token, basic_auth=basic_auth),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(response_body or "{}")
            except json.JSONDecodeError as exc:
                status = getattr(response, "status", "unknown")
                response_preview = response_body[:500]
                raise RuntimeError(f"Grafana import returned non-JSON response with HTTP {status}: {response_preview}") from exc
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grafana import failed with HTTP {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Grafana import failed: {exc.reason}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import or update a Grafana dashboard via the Grafana HTTP API.")
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--grafana-url", default="")
    parser.add_argument("--api-token", default="")
    parser.add_argument("--basic-auth", default="")
    parser.add_argument("--folder-uid", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    dashboard = json.loads(args.dashboard.read_text(encoding="utf-8"))
    payload = build_payload(
        dashboard,
        folder_uid=args.folder_uid or env_optional("GRAFANA_FOLDER_UID"),
        message=args.message,
    )

    if args.dry_run:
        result = {
            "status": "dry-run",
            "dashboard_uid": dashboard.get("uid", ""),
            "dashboard_title": dashboard.get("title", ""),
            "folder_uid": payload.get("folderUid", ""),
        }
    else:
        result = import_dashboard(
            grafana_url=args.grafana_url or env_required("GRAFANA_URL"),
            api_token=args.api_token or env_optional("GRAFANA_API_TOKEN"),
            basic_auth=args.basic_auth or env_optional("GRAFANA_BASIC_AUTH"),
            payload=payload,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
