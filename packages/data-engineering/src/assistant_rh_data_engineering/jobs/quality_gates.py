from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PYTHONPATH_ENTRIES = [
    REPO_ROOT,
    REPO_ROOT / "packages/data-engineering/src",
    REPO_ROOT / "packages/shared-config/src",
]
for entry in reversed(PYTHONPATH_ENTRIES):
    entry_str = str(entry)
    if entry_str not in sys.path:
        sys.path.insert(0, entry_str)

from assistant_rh_shared.db_helpers import DSN_ENV_KEYS, get_dsn  # noqa: E402

from assistant_rh_data_engineering.quality_gates import (  # noqa: E402
    PsycopgQualityDatabase,
    build_error_report,
    evaluate_quality_gates,
    load_quality_config,
    render_markdown_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run post-ingestion Postgres quality gates and write JSON/Markdown reports.")
    parser.add_argument("--config", default="config/data_quality_gates.json", help="Versioned quality gate configuration.")
    parser.add_argument("--target-env", choices=["staging", "prod"], required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument("--dsn-env", default="", help="Env var holding the Postgres DSN. Overrides the canonical resolution.")
    parser.add_argument("--dsn", help="Postgres DSN. Takes precedence over --dsn-env and the canonical resolution.")
    parser.add_argument("--source", action="append", default=[], required=True, help="Source to check (repeatable). Must exist in the config.")
    parser.add_argument("--blocking", action="store_true", help="Exit non-zero when any blocking quality check fails.")
    parser.add_argument("--json-output", default="", help="Write the machine-readable report to this path.")
    parser.add_argument("--markdown-output", default="", help="Write the GitHub-friendly summary to this path.")
    return parser


def validate_requested_sources(parser: argparse.ArgumentParser, args: argparse.Namespace, config: dict) -> None:
    known_sources = set(config["sources"].keys())
    for source in args.source:
        if source not in known_sources:
            parser.error(f"argument --source: invalid choice: {source!r} (choose from {_format_choices(sorted(known_sources))})")


def _format_choices(values: list[str]) -> str:
    return ", ".join(repr(value) for value in values)


def write_reports(report: dict, json_output: str, markdown_output: str) -> None:
    if json_output:
        path = Path(json_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json_dump(report), encoding="utf-8")
    markdown = render_markdown_report(report)
    if markdown_output:
        path = Path(markdown_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    print(markdown)


def _json_dump(report: dict) -> str:
    import json

    return json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"


def _resolve_dsn(args: argparse.Namespace) -> str:
    """Resolve the Postgres DSN.

    Priority: explicit --dsn, then an explicit --dsn-env override, then the
    canonical resolution shared with the rest of the data pipeline (get_dsn).
    """
    if args.dsn:
        return args.dsn
    if args.dsn_env:
        return os.getenv(args.dsn_env, "")
    try:
        return get_dsn()
    except RuntimeError:
        return ""


def run_gates(args: argparse.Namespace, config: dict) -> dict:
    """Evaluate the gates, converting expected failures into failing reports.

    Configuration errors (bad or empty manifests) and database errors both
    yield a failing report instead of a traceback, so report-only runs still
    publish diagnostics. Database error detail goes to stderr only, keeping
    DSN-adjacent strings out of the published report.
    """
    dsn = _resolve_dsn(args)
    if not dsn:
        return build_error_report(
            config,
            f"Missing Postgres DSN: pass --dsn, set --dsn-env, or define one of {', '.join(DSN_ENV_KEYS)}.",
            target_env=args.target_env,
            sources=args.source,
            blocking=args.blocking,
        )

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            db = PsycopgQualityDatabase(conn, schema=args.schema)
            return evaluate_quality_gates(
                db,
                config,
                repo_root=REPO_ROOT,
                target_env=args.target_env,
                sources=args.source,
                blocking=args.blocking,
            )
    except (FileNotFoundError, ValueError) as exc:
        return build_error_report(
            config,
            f"Quality gate configuration error: {exc}",
            target_env=args.target_env,
            sources=args.source,
            blocking=args.blocking,
            category="config",
            check_name="configuration",
            expected="valid configuration",
        )
    except (OSError, psycopg.Error) as exc:
        print(f"Postgres quality gate database error: {exc}", file=sys.stderr)
        return build_error_report(
            config,
            f"Postgres quality gate could not connect or query the database: {exc.__class__.__name__} (see job log).",
            target_env=args.target_env,
            sources=args.source,
            blocking=args.blocking,
        )


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args()
    config = load_quality_config(REPO_ROOT / args.config)
    validate_requested_sources(parser, args, config)
    report = run_gates(args, config)
    write_reports(report, args.json_output, args.markdown_output)
    return 1 if args.blocking and report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
