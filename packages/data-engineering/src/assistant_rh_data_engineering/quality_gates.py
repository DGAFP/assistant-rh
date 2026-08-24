from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg import sql

Check = dict[str, Any]


@dataclass(frozen=True)
class SourceFilter:
    column: str
    value: str


@dataclass(frozen=True)
class TableSnapshot:
    columns: set[str]
    row_count: int
    observed_ids: set[str] | None
    blank_counts: dict[str, int]
    max_timestamp: datetime | None


class QualityDatabase(Protocol):
    def inspect_table(
        self,
        table_config: dict[str, Any],
        expected_ids: list[str],
        source_filter: SourceFilter,
    ) -> TableSnapshot | None: ...


class PsycopgQualityDatabase:
    """Read each configured table once and return the metrics used by the gate."""

    def __init__(self, conn: psycopg.Connection, schema: str = "public"):
        self.conn = conn
        self.schema = schema

    def inspect_table(
        self,
        table_config: dict[str, Any],
        expected_ids: list[str],
        source_filter: SourceFilter,
    ) -> TableSnapshot | None:
        table = str(table_config["name"])
        columns = self._table_columns(table)
        if not columns:
            return None
        if table_config.get("kind") == "sections":
            return self._inspect_sections(table, columns, table_config, expected_ids, source_filter)
        return self._inspect_regular(table, columns, table_config, expected_ids, source_filter)

    def _table_columns(self, table: str) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (self.schema, table),
            )
            return {str(row[0]) for row in cur.fetchall()}

    def _inspect_regular(
        self,
        table: str,
        columns: set[str],
        table_config: dict[str, Any],
        expected_ids: list[str],
        source_filter: SourceFilter,
    ) -> TableSnapshot:
        selects: list[sql.Composable] = [sql.SQL("COUNT(*)")]
        metrics = ["row_count"]
        params: list[Any] = []

        id_column = str(table_config.get("id_column") or "")
        if id_column and id_column in columns:
            identifier = sql.Identifier(id_column)
            selects.append(sql.SQL("ARRAY_AGG(DISTINCT {}) FILTER (WHERE {} = ANY(%s))").format(identifier, identifier))
            metrics.append("observed_ids")
            params.append(expected_ids)

        for text_column in _existing_columns(table_config.get("text_columns", []), columns):
            identifier = sql.Identifier(text_column)
            selects.append(sql.SQL("COUNT(*) FILTER (WHERE {} IS NULL OR btrim({}::text) = '')").format(identifier, identifier))
            metrics.append(f"blank:{text_column}")

        freshness_column = str(table_config.get("freshness_column") or "")
        if freshness_column and freshness_column in columns:
            selects.append(sql.SQL("MAX({})").format(sql.Identifier(freshness_column)))
            metrics.append("max_timestamp")

        query = sql.SQL("SELECT {} FROM {}.{}").format(
            sql.SQL(", ").join(selects),
            sql.Identifier(self.schema),
            sql.Identifier(table),
        )
        if table_config.get("apply_source_filter"):
            query += sql.SQL(" WHERE LOWER({}) = %s").format(sql.Identifier(source_filter.column))
            params.append(source_filter.value.lower())

        return _snapshot(columns, metrics, self._fetch_one(query, params))

    def _inspect_sections(
        self,
        table: str,
        columns: set[str],
        table_config: dict[str, Any],
        expected_ids: list[str],
        source_filter: SourceFilter,
    ) -> TableSnapshot:
        selects: list[sql.Composable] = [sql.SQL("COUNT(*)"), sql.SQL("ARRAY_AGG(DISTINCT d.short_id)")]
        metrics = ["row_count", "observed_ids"]

        for text_column in _existing_columns(table_config.get("text_columns", []), columns):
            identifier = sql.Identifier(text_column)
            selects.append(sql.SQL("COUNT(*) FILTER (WHERE s.{} IS NULL OR btrim(s.{}::text) = '')").format(identifier, identifier))
            metrics.append(f"blank:{text_column}")

        freshness_column = str(table_config.get("freshness_column") or "")
        if freshness_column and freshness_column in columns:
            selects.append(sql.SQL("MAX(s.{})").format(sql.Identifier(freshness_column)))
            metrics.append("max_timestamp")

        query = sql.SQL(
            "SELECT {} FROM {}.{} s JOIN {}.rag_documents d ON d.doc_id = s.doc_id "
            "WHERE LOWER(d.{}) = %s AND d.short_id = ANY(%s)"
        ).format(
            sql.SQL(", ").join(selects),
            sql.Identifier(self.schema),
            sql.Identifier(table),
            sql.Identifier(self.schema),
            sql.Identifier(source_filter.column),
        )
        return _snapshot(columns, metrics, self._fetch_one(query, [source_filter.value.lower(), expected_ids]))

    def _fetch_one(self, query: sql.SQL | sql.Composed, params: list[Any]) -> tuple[Any, ...]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("Quality gate aggregate query returned no row.")
        return tuple(row)


def _existing_columns(configured: Any, columns: set[str]) -> list[str]:
    return [str(column) for column in configured if str(column) in columns]


def _snapshot(columns: set[str], metrics: list[str], row: tuple[Any, ...]) -> TableSnapshot:
    values = dict(zip(metrics, row, strict=True))
    raw_ids = values.get("observed_ids")
    observed_ids = {str(value) for value in raw_ids or []} if "observed_ids" in values else None
    blank_counts = {metric.removeprefix("blank:"): int(values[metric]) for metric in metrics if metric.startswith("blank:")}
    timestamp = values.get("max_timestamp")
    return TableSnapshot(
        columns=columns,
        row_count=int(values["row_count"]),
        observed_ids=observed_ids,
        blank_counts=blank_counts,
        max_timestamp=timestamp if isinstance(timestamp, datetime) else None,
    )


def load_quality_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Quality gate config must be a JSON object.")
    if not isinstance(payload.get("sources"), dict):
        raise ValueError("Quality gate config must define a sources object.")
    return payload


def resolve_expected_ids(repo_root: Path, source_name: str, source_config: dict[str, Any]) -> list[str]:
    spec = source_config.get("expected_ids")
    if not isinstance(spec, dict):
        raise ValueError(f"Source {source_name!r} configuration is missing 'expected_ids'.")
    if not spec.get("path"):
        raise ValueError(f"Source {source_name!r} 'expected_ids' configuration is missing 'path'.")
    if not spec.get("field"):
        raise ValueError(f"Source {source_name!r} 'expected_ids' configuration is missing 'field'.")

    path = repo_root / str(spec["path"])
    if not path.is_file():
        raise FileNotFoundError(f"Expected IDs file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc
    if payload is None:
        raise ValueError(f"JSON file {path} resolved to null.")

    field = str(spec["field"])
    values = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError(f"Expected ID field {field!r} in {path} must be a list.")
    return _normalize_ids(source_name, values)


def _normalize_ids(source_name: str, values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if source_name == "service_public":
            normalized = normalized.upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return output


def evaluate_quality_gates(
    db: QualityDatabase,
    config: dict[str, Any],
    *,
    repo_root: Path,
    target_env: str,
    sources: list[str],
    blocking: bool,
) -> dict[str, Any]:
    selected_sources = sources or sorted(config["sources"])
    checks: list[Check] = []
    for source_name in selected_sources:
        source_config = config["sources"][source_name]
        expected_ids = resolve_expected_ids(repo_root, source_name, source_config)
        source_filter = _source_filter(source_config)
        for table_config in source_config.get("tables", []):
            checks.extend(_evaluate_table(db, source_name, source_config, table_config, expected_ids, source_filter, target_env))
    return build_report(config, checks, target_env=target_env, sources=selected_sources, blocking=blocking)


def _evaluate_table(
    db: QualityDatabase,
    source_name: str,
    source_config: dict[str, Any],
    table_config: dict[str, Any],
    expected_ids: list[str],
    source_filter: SourceFilter,
    target_env: str,
) -> list[Check]:
    table = str(table_config["name"])
    snapshot = db.inspect_table(table_config, expected_ids, source_filter)
    if snapshot is None:
        return [_check(source_name, table, "table_exists", False, "missing", "present", "Table is missing.")]

    checks = [_check(source_name, table, "table_exists", True, "present", "present", "Table is present.")]
    min_rows = (
        int(table_config["min_rows"])
        if "min_rows" in table_config
        else _minimum(len(expected_ids), table_config.get("min_rows_per_expected_id", 1))
    )
    rows_ok = snapshot.row_count >= min_rows
    checks.append(
        _check(
            source_name,
            table,
            "min_rows",
            rows_ok,
            snapshot.row_count,
            min_rows,
            "Table row count meets the minimum." if rows_ok else "Table row count is below the minimum.",
        )
    )

    kind = str(table_config.get("kind") or "table")
    id_column = str(table_config.get("id_column") or "")
    coverage_name = "expected_document_coverage" if kind == "sections" else "expected_id_coverage"
    if kind == "sections" or id_column:
        if id_column and id_column not in snapshot.columns:
            checks.append(_check(source_name, table, coverage_name, False, "missing column", id_column, "ID column is missing."))
        else:
            checks.append(_coverage_check(source_name, table, coverage_name, snapshot.observed_ids or set(), expected_ids, table_config))

    for text_column in map(str, table_config.get("text_columns", [])):
        check_name = f"text_not_blank:{text_column}"
        if text_column not in snapshot.columns:
            checks.append(_check(source_name, table, check_name, False, "missing column", "column present", "Text column is missing."))
            continue
        blank_count = snapshot.blank_counts[text_column]
        checks.append(
            _check(
                source_name,
                table,
                check_name,
                blank_count == 0,
                blank_count,
                0,
                "Text column has no blank rows." if blank_count == 0 else "Text column contains blank rows.",
            )
        )

    freshness_column = str(table_config.get("freshness_column") or "")
    if freshness_column:
        checks.append(_freshness_check(source_name, table, freshness_column, snapshot, source_config, target_env))
    return checks


def _coverage_check(
    source: str,
    table: str,
    name: str,
    observed_ids: set[str],
    expected_ids: list[str],
    table_config: dict[str, Any],
) -> Check:
    expected_min = _minimum(len(expected_ids), table_config.get("min_distinct_expected_id_ratio", 1))
    missing = [expected_id for expected_id in expected_ids if expected_id not in observed_ids]
    passed = len(observed_ids) >= expected_min
    if name == "expected_document_coverage":
        message = "Expected documents have sections." if passed else "Some expected documents have no sections."
    else:
        message = "Expected IDs are covered." if passed else "Some expected IDs are missing."
    return _check(
        source,
        table,
        name,
        passed,
        len(observed_ids),
        expected_min,
        message,
        {"missing_count": len(missing), "missing_sample": missing[:20]},
    )


def _freshness_check(
    source: str,
    table: str,
    column: str,
    snapshot: TableSnapshot,
    source_config: dict[str, Any],
    target_env: str,
) -> Check:
    max_age = _freshness_hours(source_config, target_env)
    if column not in snapshot.columns:
        return _check(source, table, "freshness", False, "missing column", "freshness column present", "Freshness column is missing.")
    if snapshot.max_timestamp is None:
        return _check(source, table, "freshness", False, "null", f"<= {max_age}h", "No timestamp found.")
    age = round(_age_hours(snapshot.max_timestamp), 2)
    passed = age <= max_age
    return _check(
        source,
        table,
        "freshness",
        passed,
        age,
        f"<= {max_age}h",
        "Max updated_at is recent enough." if passed else "Max updated_at is too old.",
    )


def build_error_report(config: dict[str, Any], message: str, *, target_env: str, sources: list[str], blocking: bool) -> dict[str, Any]:
    check = _check("database", "-", "connection", False, "error", "connection ok", message, check_id="database.connection")
    return build_report(config, [check], target_env=target_env, sources=sources, blocking=blocking)


def build_report(
    config: dict[str, Any],
    checks: list[Check],
    *,
    target_env: str,
    sources: list[str],
    blocking: bool,
) -> dict[str, Any]:
    counts = {status: sum(check["status"] == status for check in checks) for status in ("pass", "fail")}
    return {
        "version": config.get("version", 1),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "target_env": target_env,
        "blocking": blocking,
        "status": "fail" if counts["fail"] else "pass",
        "sources": sources,
        "summary": counts,
        "checks": checks,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Data quality gate: {str(report['status']).upper()}",
        "",
        f"- Target env: `{report['target_env']}`",
        f"- Mode: `{'blocking' if report.get('blocking') else 'report-only'}`",
        f"- Sources: `{', '.join(report.get('sources') or []) or 'none'}`",
        f"- Generated at: `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Passed: `{report['summary']['pass']}`",
        f"- Failed: `{report['summary']['fail']}`",
    ]
    lines.extend(_render_check_table("Failing checks", [check for check in report["checks"] if check["status"] == "fail"]))
    return "\n".join(lines) + "\n"


def _render_check_table(title: str, checks: list[Check]) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not checks:
        return [*lines, "None."]
    lines.extend(["| Source | Table | Check | Observed | Expected | Message |", "|---|---|---|---:|---:|---|"])
    for check in checks:
        cells = (_md_cell(check[key]) for key in ("source", "table", "check", "observed", "expected", "message"))
        lines.append("| {} | {} | {} | {} | {} | {} |".format(*cells))
    return lines


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _source_filter(source_config: dict[str, Any]) -> SourceFilter:
    payload = source_config.get("source_filter") or {}
    return SourceFilter(column=str(payload.get("column") or "source"), value=str(payload["value"]))


def _freshness_hours(source_config: dict[str, Any], target_env: str) -> int:
    value = source_config.get("freshness_max_age_hours", {})
    if isinstance(value, dict):
        return int(value.get(target_env) or value.get("default") or 1080)
    return int(value)


def _age_hours(value: datetime) -> float:
    timestamp = value if value.tzinfo else value.replace(tzinfo=UTC)
    return (datetime.now(tz=UTC) - timestamp.astimezone(UTC)).total_seconds() / 3600


def _minimum(total: int, ratio: Any) -> int:
    return math.ceil(total * float(ratio))


def _check(
    source: str,
    table: str,
    name: str,
    passed: bool,
    observed: Any,
    expected: Any,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    check_id: str | None = None,
) -> Check:
    return {
        "id": check_id or f"{source}.{table}.{name}",
        "source": source,
        "table": table,
        "check": name,
        "status": "pass" if passed else "fail",
        "observed": observed,
        "expected": expected,
        "message": message,
        "details": details or {},
    }
