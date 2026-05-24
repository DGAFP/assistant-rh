"""Summarize conformance stage reports into markdown + JSON.

Usage:

  python scripts/summarize_conformance_reports.py \
    --reports-dir tests/conformance/reports/ci \
    --thresholds-file tests/conformance/thresholds.replay.json \
    --output-markdown /tmp/conformance-summary.md \
    --output-json /tmp/conformance-summary.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StageConfig:
    stage: str
    required: bool
    summary_key: str | None = None
    threshold_key: str | None = None


STAGES: list[StageConfig] = [
    StageConfig("retriever", True, "retrievalOverlapTopKAvg", "retrieval_overlap_topk_avg"),
    StageConfig("section-aggregator", True, "sectionOverlapTopKAvg", "section_overlap_topk_avg"),
    StageConfig("context-selector", True, "selectorOverlapTopKAvg", "selector_overlap_topk_avg"),
    StageConfig("context-builder", True, "contextOverlapTopKAvg", "context_overlap_topk_avg"),
    StageConfig("query-processor", True, "intentMatchRate", "intent_match_rate"),
    StageConfig("rag-pipeline", False),
]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_report(reports_dir: Path, stage: str) -> Path | None:
    direct = reports_dir / f"{stage}.json"
    if direct.exists():
        return direct

    matches = sorted(reports_dir.glob(f"**/{stage}.json"))
    if matches:
        return matches[0]
    return None


def _is_retriever_infra_skip(report: dict[str, Any]) -> bool:
    summary = report.get("summary") or {}
    if summary.get("retrievalOverlapTopKAvg") is not None:
        return False

    errors = report.get("errors") or []
    if not isinstance(errors, list) or not errors:
        return False

    return all(isinstance(row, dict) and "Missing required dual-index table(s)" in str((row.get("error") or "")) for row in errors)


def _fmt_float(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _stage_status(
    config: StageConfig,
    report: dict[str, Any] | None,
    thresholds: dict[str, float],
) -> tuple[str, str, str, str]:
    """Returns (status, metric, threshold, note)."""
    if report is None:
        return ("❌ missing", "-", "-", "report not found")

    if report.get("skipped") is True:
        reason = str(report.get("skipReason") or "stage skipped")
        expected = thresholds.get(config.threshold_key) if config.threshold_key else None
        return ("⚠️ skipped", "-", _fmt_float(expected), reason)

    summary = report.get("summary") or {}
    if config.required and config.summary_key and config.threshold_key:
        actual = summary.get(config.summary_key)
        expected = thresholds.get(config.threshold_key)

        if actual is None:
            if config.stage == "retriever" and _is_retriever_infra_skip(report):
                return ("⚠️ skipped", "-", _fmt_float(expected), "dual-index tables missing")
            return ("❌ missing", "-", _fmt_float(expected), f"missing {config.summary_key}")

        if expected is None:
            return ("❌ config", _fmt_float(actual), "-", f"missing {config.threshold_key}")

        actual_f = float(actual)
        expected_f = float(expected)
        if actual_f >= expected_f:
            return ("✅ pass", _fmt_float(actual_f), _fmt_float(expected_f), "")
        return ("❌ fail", _fmt_float(actual_f), _fmt_float(expected_f), "below threshold")

    failed_count = report.get("failedCount")
    if isinstance(failed_count, int) and failed_count > 0:
        return ("⚠️ issues", "-", "-", f"failedCount={failed_count}")

    return ("ℹ️ info", "-", "-", "informational")


def build_summary(reports_dir: Path, thresholds: dict[str, float]) -> tuple[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    required_failures = 0

    for config in STAGES:
        report_path = _find_report(reports_dir, config.stage)
        report = _load_json(report_path) if report_path else None

        status, metric, threshold, note = _stage_status(config, report, thresholds)
        if config.required and status.startswith("❌"):
            required_failures += 1

        rows.append(
            {
                "stage": config.stage,
                "required": config.required,
                "status": status,
                "metric": metric,
                "threshold": threshold,
                "note": note,
                "report_path": str(report_path) if report_path else None,
            }
        )

    markdown_lines = [
        "| Stage | Type | Status | Metric | Threshold | Notes |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        kind = "required" if row["required"] else "informational"
        markdown_lines.append(f"| {row['stage']} | {kind} | {row['status']} | {row['metric']} | {row['threshold']} | {row['note'] or ''} |")

    markdown = "\n".join(markdown_lines)
    payload = {
        "required_failures": required_failures,
        "rows": rows,
    }
    return markdown, payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize conformance reports")
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--thresholds-file", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    reports_dir = args.reports_dir.resolve()
    thresholds_file = args.thresholds_file.resolve()
    thresholds = _load_json(thresholds_file)

    markdown, payload = build_summary(reports_dir, thresholds)

    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown + "\n", encoding="utf-8")
    else:
        print(markdown)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
