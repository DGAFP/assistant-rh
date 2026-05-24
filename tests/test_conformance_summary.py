from __future__ import annotations

import json

from scripts.summarize_conformance_reports import build_summary


def _write_report(tmp_path, stage: str, payload: dict) -> None:
    (tmp_path / f"{stage}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_build_summary_treats_skipped_required_stage_as_non_failure(tmp_path):
    skip_reason = "Repository DB secrets are unavailable to Dependabot pull_request workflows."
    thresholds = {
        "retrieval_overlap_topk_avg": 0.8,
        "section_overlap_topk_avg": 0.8,
        "selector_overlap_topk_avg": 0.7,
        "context_overlap_topk_avg": 0.5,
        "intent_match_rate": 0.95,
    }

    for stage in ("retriever", "context-selector", "context-builder"):
        _write_report(
            tmp_path,
            stage,
            {
                "skipped": True,
                "skipReason": skip_reason,
                "summary": {},
                "failedCount": 0,
            },
        )

    _write_report(tmp_path, "section-aggregator", {"summary": {"sectionOverlapTopKAvg": 1.0}, "failedCount": 0})
    _write_report(tmp_path, "query-processor", {"summary": {"intentMatchRate": 1.0}, "failedCount": 0})
    _write_report(tmp_path, "rag-pipeline", {"summary": {}, "failedCount": 0})

    markdown, payload = build_summary(tmp_path, thresholds)

    assert payload["required_failures"] == 0
    assert "⚠️ skipped" in markdown
    assert "0.8000" in markdown
    assert "0.7000" in markdown
    assert "0.5000" in markdown
    assert skip_reason in markdown
