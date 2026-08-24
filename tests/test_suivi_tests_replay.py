"""Tests du CLI de rejeu Suivi-Tests (issue #298)."""

from __future__ import annotations

import argparse
import json
import re
from types import SimpleNamespace

import pytest

from scripts import suivi_tests_replay as replay
from src.suivi_tests.campaign import CampaignQuestion


def test_expected_patterns_round_trip_in_filters() -> None:
    encoded = replay._filters_with_expected_patterns('{"selector": true}', ["Doc Grist", "86-83"])

    assert json.loads(encoded) == {
        "selector": True,
        replay.EXPECTED_PATTERNS_FILTER_KEY: ["Doc Grist", "86-83"],
    }
    assert replay._stored_expected_patterns(encoded) == ["Doc Grist", "86-83"]
    assert replay._stored_expected_patterns('{"selector": true}') is None


def test_report_expected_docs_uses_grist_for_legacy_rows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "expected.json"
    config.write_text(json.dumps({"61": ["ancien JSON"]}), encoding="utf-8")
    args = argparse.Namespace(expected_config=str(config), grist_doc=None, grist_table=None)
    rows = [{"conversation_id": "masa-61", "filters": "{}"}]
    question = CampaignQuestion(61, "Question", "masa", "masa", ["attendu Grist"])
    monkeypatch.setattr(replay, "fetch_campaign_questions", lambda **kwargs: [question])

    assert replay._load_report_expected_docs(args, rows) == {61: ["attendu Grist"]}


def test_report_does_not_reread_grist_when_run_has_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "expected.json"
    config.write_text("{}", encoding="utf-8")
    args = argparse.Namespace(expected_config=str(config), grist_doc=None, grist_table=None)
    filters = replay._filters_with_expected_patterns({}, ["attendu figé"])
    rows = [{"conversation_id": "masa-61", "filters": filters}]

    def fail_if_called(**kwargs):
        raise AssertionError("Grist ne doit pas être relu")

    monkeypatch.setattr(replay, "fetch_campaign_questions", fail_if_called)
    assert replay._load_report_expected_docs(args, rows) == {}
    assert replay._stored_expected_patterns(filters) == ["attendu figé"]


def test_ids_option_requires_at_least_one_record() -> None:
    with pytest.raises(SystemExit):
        replay.build_parser().parse_args(["run", "--ids"])


def test_default_session_id_is_unique_and_timestamped(monkeypatch: pytest.MonkeyPatch) -> None:
    suffixes = iter(["aaaaaa0", "bbbbbb0"])
    monkeypatch.setattr(replay.uuid, "uuid4", lambda: SimpleNamespace(hex=next(suffixes)))

    first = replay._default_session_id()
    second = replay._default_session_id()

    assert re.fullmatch(r"suivi-tests-\d{8}-\d{6}-[0-9a-f]{6}", first)
    assert first.endswith("-aaaaaa")
    assert second.endswith("-bbbbbb")
    assert first != second


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    def __init__(self, values):
        self.values = iter(values)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, statement, params):
        return _ScalarResult(next(self.values))


class _Engine:
    def __init__(self, values):
        self.values = values

    def connect(self):
        return _Connection(self.values)


def test_logging_confirmation_accepts_complete_write() -> None:
    replay._assert_logging_persisted(_Engine([True, 3]), turn_id="turn-1", expected_trace_events=3)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([False, 0], "chat_runs non persisté"),
        ([True, 1], "rag_trace_events incomplets"),
    ],
)
def test_logging_confirmation_rejects_partial_write(values, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        replay._assert_logging_persisted(_Engine(values), turn_id="turn-1", expected_trace_events=2)
