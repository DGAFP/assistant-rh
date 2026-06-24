from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg import sql


@dataclass(frozen=True)
class SourceFilter:
    column: str
    value: str


@dataclass(frozen=True)
class CheckResult:
    id: str
    source: str
    table: str
    check: str
    status: str
    observed: Any
    expected: Any
    message: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "table": self.table,
            "check": self.check,
            "status": self.status,
            "observed": self.observed,
            "expected": self.expected,
            "message": self.message,
            "details": self.details,
        }


class QualityDatabase(Protocol):
    def table_columns(self, table: str) -> set[str]: ...

    def row_count(self, table: str, source_filter: SourceFilter | None = None) -> int: ...

    def distinct_expected_count(self, table: str, id_column: str, expected_ids: list[str], source_filter: SourceFilter | None = None) -> int: ...

    def missing_expected_ids(
        self,
        table: str,
        id_column: str,
        expected_ids: list[str],
        source_filter: SourceFilter | None = None,
    ) -> list[str]: ...

    def blank_text_count(self, table: str, text_column: str, source_filter: SourceFilter | None = None) -> int: ...

    def max_timestamp(self, table: str, column: str, source_filter: SourceFilter | None = None) -> datetime | None: ...

    def section_row_count_for_documents(self, expected_ids: list[str], document_source: SourceFilter) -> int: ...

    def section_distinct_document_count(self, expected_ids: list[str], document_source: SourceFilter) -> int: ...

    def missing_section_document_ids(self, expected_ids: list[str], document_source: SourceFilter) -> list[str]: ...

    def blank_section_text_count(self, text_column: str, expected_ids: list[str], document_source: SourceFilter) -> int: ...


class PsycopgQualityDatabase:
    def __init__(self, conn: psycopg.Connection, schema: str = "public"):
        self.conn = conn
        self.schema = schema

    def table_columns(self, table: str) -> set[str]:
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

    def row_count(self, table: str, source_filter: SourceFilter | None = None) -> int:
        query, params = self._table_query(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(self.schema), sql.Identifier(table)),
            source_filter,
        )
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def distinct_expected_count(self, table: str, id_column: str, expected_ids: list[str], source_filter: SourceFilter | None = None) -> int:
        if not expected_ids:
            return 0
        base = sql.SQL("SELECT COUNT(DISTINCT {}) FROM {}.{}").format(
            sql.Identifier(id_column),
            sql.Identifier(self.schema),
            sql.Identifier(table),
        )
        query, params = self._table_query(base, source_filter, extra_where=sql.SQL("{} = ANY(%s)").format(sql.Identifier(id_column)))
        params.append(expected_ids)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def missing_expected_ids(
        self,
        table: str,
        id_column: str,
        expected_ids: list[str],
        source_filter: SourceFilter | None = None,
    ) -> list[str]:
        if not expected_ids:
            return []
        base = sql.SQL("SELECT DISTINCT {} FROM {}.{}").format(
            sql.Identifier(id_column),
            sql.Identifier(self.schema),
            sql.Identifier(table),
        )
        query, params = self._table_query(base, source_filter, extra_where=sql.SQL("{} = ANY(%s)").format(sql.Identifier(id_column)))
        params.append(expected_ids)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            observed = {str(row[0]) for row in cur.fetchall() if row[0] is not None}
        return [expected_id for expected_id in expected_ids if expected_id not in observed]

    def blank_text_count(self, table: str, text_column: str, source_filter: SourceFilter | None = None) -> int:
        base = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(self.schema), sql.Identifier(table))
        condition = sql.SQL("({} IS NULL OR btrim({}::text) = '')").format(sql.Identifier(text_column), sql.Identifier(text_column))
        query, params = self._table_query(base, source_filter, extra_where=condition)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def max_timestamp(self, table: str, column: str, source_filter: SourceFilter | None = None) -> datetime | None:
        base = sql.SQL("SELECT MAX({}) FROM {}.{}").format(
            sql.Identifier(column),
            sql.Identifier(self.schema),
            sql.Identifier(table),
        )
        query, params = self._table_query(base, source_filter)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            value = cur.fetchone()[0]
        return value if isinstance(value, datetime) else None

    def section_row_count_for_documents(self, expected_ids: list[str], document_source: SourceFilter) -> int:
        query, params = self._sections_query(sql.SQL("COUNT(*)"), expected_ids, document_source)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def section_distinct_document_count(self, expected_ids: list[str], document_source: SourceFilter) -> int:
        query, params = self._sections_query(sql.SQL("COUNT(DISTINCT d.short_id)"), expected_ids, document_source)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def missing_section_document_ids(self, expected_ids: list[str], document_source: SourceFilter) -> list[str]:
        query, params = self._sections_query(sql.SQL("DISTINCT d.short_id"), expected_ids, document_source)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            observed = {str(row[0]) for row in cur.fetchall() if row[0] is not None}
        return [expected_id for expected_id in expected_ids if expected_id not in observed]

    def blank_section_text_count(self, text_column: str, expected_ids: list[str], document_source: SourceFilter) -> int:
        select_sql = sql.SQL("COUNT(*)")
        extra = sql.SQL("(s.{} IS NULL OR btrim(s.{}::text) = '')").format(sql.Identifier(text_column), sql.Identifier(text_column))
        query, params = self._sections_query(select_sql, expected_ids, document_source, extra_where=extra)
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def _table_query(
        self,
        base: sql.Composed,
        source_filter: SourceFilter | None,
        extra_where: sql.Composed | sql.SQL | None = None,
    ) -> tuple[sql.Composed, list[Any]]:
        conditions: list[sql.Composed | sql.SQL] = []
        params: list[Any] = []
        if source_filter is not None:
            conditions.append(sql.SQL("LOWER({}) = %s").format(sql.Identifier(source_filter.column)))
            params.append(source_filter.value.lower())
        if extra_where is not None:
            conditions.append(extra_where)
        if not conditions:
            return base, params
        return base + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions), params

    def _sections_query(
        self,
        select_sql: sql.SQL,
        expected_ids: list[str],
        document_source: SourceFilter,
        extra_where: sql.Composed | sql.SQL | None = None,
    ) -> tuple[sql.Composed, list[Any]]:
        conditions: list[sql.Composed | sql.SQL] = [
            sql.SQL("LOWER(d.{}) = %s").format(sql.Identifier(document_source.column)),
            sql.SQL("d.short_id = ANY(%s)"),
        ]
        params: list[Any] = [document_source.value.lower(), expected_ids]
        if extra_where is not None:
            conditions.append(extra_where)
        query = sql.SQL("SELECT {} FROM {}.{} s JOIN {}.{} d ON d.doc_id = s.doc_id WHERE {}").format(
            select_sql,
            sql.Identifier(self.schema),
            sql.Identifier("rag_sections"),
            sql.Identifier(self.schema),
            sql.Identifier("rag_documents"),
            sql.SQL(" AND ").join(conditions),
        )
        return query, params


def load_quality_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Quality gate config must be a JSON object.")
    if "sources" not in payload or not isinstance(payload["sources"], dict):
        raise ValueError("Quality gate config must define a sources object.")
    return payload


def resolve_expected_ids(repo_root: Path, source_name: str, source_config: dict[str, Any]) -> list[str]:
    spec = source_config.get("expected_ids")
    if not isinstance(spec, dict):
        raise ValueError(f"Source {source_name!r} configuration is missing 'expected_ids'.")

    path_value = spec.get("path")
    if not path_value:
        raise ValueError(f"Source {source_name!r} 'expected_ids' configuration is missing 'path'.")

    field_value = spec.get("field")
    if not field_value:
        raise ValueError(f"Source {source_name!r} 'expected_ids' configuration is missing 'field'.")
    field = str(field_value)

    path = repo_root / str(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"Expected IDs file not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc
    else:
        if payload is None:
            raise ValueError(f"JSON file {path} resolved to null.")
        values = payload.get(field) if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise ValueError(f"Expected ID field {field!r} in {path} must be a list.")
        return normalize_ids(source_name, values)


def normalize_ids(source_name: str, values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if source_name == "service_public":
            normalized = normalized.upper()
        if not normalized or normalized in seen:
            continue
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
    selected_sources = _selected_sources(config, sources)
    checks: list[CheckResult] = []
    for source_name in selected_sources:
        source_config = config["sources"][source_name]
        expected_ids = resolve_expected_ids(repo_root, source_name, source_config)
        checks.extend(_evaluate_source_tables(db, source_name, source_config, expected_ids, target_env))

    return build_report(
        config,
        checks,
        target_env=target_env,
        sources=selected_sources,
        blocking=blocking,
    )


def build_error_report(config: dict[str, Any], message: str, *, target_env: str, sources: list[str], blocking: bool) -> dict[str, Any]:
    check = CheckResult(
        id="database.connection",
        source="database",
        table="-",
        check="connection",
        status="fail",
        observed="error",
        expected="connection ok",
        message=message,
        details={},
    )
    return build_report(
        config,
        [check],
        target_env=target_env,
        sources=sources,
        blocking=blocking,
    )


def build_report(
    config: dict[str, Any],
    checks: list[CheckResult],
    *,
    target_env: str,
    sources: list[str],
    blocking: bool,
) -> dict[str, Any]:
    counts = {status: sum(1 for check in checks if check.status == status) for status in ("pass", "fail", "warn", "skip")}
    return {
        "version": config.get("version", 1),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "target_env": target_env,
        "blocking": blocking,
        "status": "fail" if counts["fail"] else "pass",
        "sources": sources,
        "summary": counts,
        "checks": [check.as_dict() for check in checks],
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    status = str(report["status"]).upper()
    mode = "blocking" if report.get("blocking") else "report-only"
    lines = [
        f"# Data quality gate: {status}",
        "",
        f"- Target env: `{report['target_env']}`",
        f"- Mode: `{mode}`",
        f"- Sources: `{', '.join(report.get('sources') or []) or 'none'}`",
        f"- Generated at: `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Passed: `{report['summary']['pass']}`",
        f"- Failed: `{report['summary']['fail']}`",
        f"- Warnings: `{report['summary']['warn']}`",
        f"- Skipped: `{report['summary']['skip']}`",
    ]
    failures = [check for check in report["checks"] if check["status"] == "fail"]
    warnings = [check for check in report["checks"] if check["status"] in {"warn", "skip"}]
    lines.extend(_render_check_table("Failing checks", failures))
    lines.extend(_render_check_table("Warnings and skipped checks", warnings))
    return "\n".join(lines) + "\n"


def _render_check_table(title: str, checks: list[dict[str, Any]]) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not checks:
        lines.append("None.")
        return lines
    lines.extend(["| Source | Table | Check | Observed | Expected | Message |", "|---|---|---|---:|---:|---|"])
    for check in checks:
        lines.append(
            "| {source} | {table} | {name} | {observed} | {expected} | {message} |".format(
                source=_md_cell(check["source"]),
                table=_md_cell(check["table"]),
                name=_md_cell(check["check"]),
                observed=_md_cell(check["observed"]),
                expected=_md_cell(check["expected"]),
                message=_md_cell(check["message"]),
            )
        )
    return lines


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _selected_sources(config: dict[str, Any], sources: list[str]) -> list[str]:
    if sources:
        return sources
    return sorted(config["sources"].keys())


def _evaluate_source_tables(
    db: QualityDatabase,
    source_name: str,
    source_config: dict[str, Any],
    expected_ids: list[str],
    target_env: str,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    source_filter = _source_filter(source_config)
    for table_config in source_config.get("tables", []):
        table = str(table_config["name"])
        kind = str(table_config.get("kind") or "table")
        columns = db.table_columns(table)
        if not columns:
            checks.append(_check(source_name, table, "table_exists", False, "missing", "present", "Table is missing."))
            continue
        checks.append(_check(source_name, table, "table_exists", True, "present", "present", "Table is present."))
        if kind == "sections":
            checks.extend(_evaluate_sections_table(db, source_name, table_config, expected_ids, source_filter))
        else:
            table_source_filter = source_filter if table_config.get("apply_source_filter") else None
            checks.extend(_evaluate_regular_table(db, source_name, table_config, expected_ids, table_source_filter))
        checks.extend(
            _evaluate_text_columns(
                db,
                source_name,
                table,
                table_config,
                columns,
                source_filter if table_config.get("apply_source_filter") else None,
                expected_ids,
                source_filter,
            )
        )
        freshness_source_filter = source_filter if table_config.get("apply_source_filter") else None
        checks.extend(_evaluate_freshness(db, source_name, table, table_config, columns, source_config, target_env, freshness_source_filter))
    return checks


def _evaluate_regular_table(
    db: QualityDatabase,
    source_name: str,
    table_config: dict[str, Any],
    expected_ids: list[str],
    source_filter: SourceFilter | None,
) -> list[CheckResult]:
    table = str(table_config["name"])
    id_column = str(table_config.get("id_column") or "")
    min_rows = _minimum_expected(len(expected_ids), float(table_config.get("min_rows_per_expected_id", 1)))
    row_count = db.row_count(table, source_filter)
    checks = [_check(source_name, table, "min_rows", row_count >= min_rows, row_count, min_rows, "Table row count meets the minimum.")]
    if id_column:
        ratio = float(table_config.get("min_distinct_expected_id_ratio", 1))
        expected_min = _minimum_expected(len(expected_ids), ratio)
        distinct_count = db.distinct_expected_count(table, id_column, expected_ids, source_filter)
        missing = db.missing_expected_ids(table, id_column, expected_ids, source_filter)
        checks.append(
            _check(
                source_name,
                table,
                "expected_id_coverage",
                distinct_count >= expected_min,
                distinct_count,
                expected_min,
                "Expected IDs are covered.",
                {"missing_count": len(missing), "missing_sample": missing[:20]},
            )
        )
    return checks


def _evaluate_sections_table(
    db: QualityDatabase,
    source_name: str,
    table_config: dict[str, Any],
    expected_ids: list[str],
    source_filter: SourceFilter,
) -> list[CheckResult]:
    table = str(table_config["name"])
    min_rows = _minimum_expected(len(expected_ids), float(table_config.get("min_rows_per_expected_id", 1)))
    row_count = db.section_row_count_for_documents(expected_ids, source_filter)
    distinct_count = db.section_distinct_document_count(expected_ids, source_filter)
    expected_min = _minimum_expected(len(expected_ids), float(table_config.get("min_distinct_expected_id_ratio", 1)))
    missing = db.missing_section_document_ids(expected_ids, source_filter)
    return [
        _check(source_name, table, "min_rows", row_count >= min_rows, row_count, min_rows, "Linked section row count meets the minimum."),
        _check(
            source_name,
            table,
            "expected_document_coverage",
            distinct_count >= expected_min,
            distinct_count,
            expected_min,
            "Expected documents have sections.",
            {"missing_count": len(missing), "missing_sample": missing[:20]},
        ),
    ]


def _evaluate_text_columns(
    db: QualityDatabase,
    source_name: str,
    table: str,
    table_config: dict[str, Any],
    columns: set[str],
    source_filter: SourceFilter | None,
    expected_ids: list[str],
    document_source: SourceFilter,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for text_column in table_config.get("text_columns", []):
        if text_column not in columns:
            checks.append(
                _check(source_name, table, f"text_not_blank:{text_column}", False, "missing column", "column present", "Text column is missing.")
            )
            continue
        if table_config.get("kind") == "sections":
            blank_count = db.blank_section_text_count(text_column, expected_ids, document_source)
            checks.append(
                _check(source_name, table, f"text_not_blank:{text_column}", blank_count == 0, blank_count, 0, "Text column has no blank rows.")
            )
            continue
        blank_count = db.blank_text_count(table, text_column, source_filter)
        checks.append(_check(source_name, table, f"text_not_blank:{text_column}", blank_count == 0, blank_count, 0, "Text column has no blank rows."))
    return checks


def _evaluate_freshness(
    db: QualityDatabase,
    source_name: str,
    table: str,
    table_config: dict[str, Any],
    columns: set[str],
    source_config: dict[str, Any],
    target_env: str,
    source_filter: SourceFilter | None,
) -> list[CheckResult]:
    freshness_column = table_config.get("freshness_column")
    if not freshness_column:
        return []
    freshness_column = str(freshness_column)
    if freshness_column not in columns:
        return [_check(source_name, table, "freshness", False, "missing column", "freshness column present", "Freshness column is missing.")]
    max_age_hours = _freshness_hours(source_config, target_env)
    value = db.max_timestamp(table, freshness_column, source_filter)
    if value is None:
        return [_check(source_name, table, "freshness", False, "null", f"<= {max_age_hours}h", "No timestamp found.")]
    observed_age = round(_age_hours(value), 2)
    return [
        _check(
            source_name,
            table,
            "freshness",
            observed_age <= max_age_hours,
            observed_age,
            f"<= {max_age_hours}h",
            "Max updated_at is recent enough.",
        )
    ]


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


def _minimum_expected(total: int, ratio: float) -> int:
    return math.ceil(total * ratio)


def _check(
    source: str,
    table: str,
    name: str,
    passed: bool,
    observed: Any,
    expected: Any,
    message: str,
    details: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        id=f"{source}.{table}.{name}",
        source=source,
        table=table,
        check=name,
        status="pass" if passed else "fail",
        observed=observed,
        expected=expected,
        message=message,
        details=details or {},
    )
