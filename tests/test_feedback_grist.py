"""#561: Grist protocol, source mapping and human annotation ownership."""

from copy import deepcopy
from itertools import product

import pandas as pd
import pytest
import requests
from assistant_rh_rag_pipeline.feedback_grist import (
    HUMAN_COLUMNS,
    FeedbackGristClient,
    FeedbackGristConfig,
    FeedbackGristError,
    build_feedback_records,
    feedback_columns,
    feedback_source_environment,
)


def feedbacks():
    return pd.DataFrame(
        [
            {
                "id": 42,
                "turn_id": "turn-1",
                "ts": pd.Timestamp("2026-09-15T10:00:00+02:00"),
                "question": "Ma question",
                "answer": "Réponse complète. " * 1000,
                "stars": 0,
                "helpful": False,
                "reasons_positive": "claire;rapide",
                "reasons_negative": "incomplète",
                "comment": "À revoir",
                "selected_ministry": "MI",
                "user_group": "testers",
                "beta_scope": "Non",
                "error_category": "missing_document",
                "ai_reason": "Hypothèse automatique",
                "theme": "conges",
                "rag_version": "v3",
                "dist_after_rerank": {"MI": 2},
                # Even if present upstream, annotations must not be sent.
                "traite": True,
            },
            {"id": 43, "question": "Sans run ni analyse", "answer": "Réponse conservée"},
        ]
    )


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return deepcopy(self.payload)


class GristServer:
    """Stateful double of the documented Grist schema + require/fields upsert.

    Failures can occur AFTER a durable write, as with a dropped HTTP reply.
    """

    def __init__(self):
        self.columns = None
        self.rows = {}
        self.calls = []
        self.fail_after = None
        self.fail_before = None
        self.put_count = 0

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if self.fail_before == method:
            self.fail_before = None
            return Response(status=403)
        if method == "GET" and url.endswith("/tables"):
            return Response({"tables": [] if self.columns is None else [{"id": "Feedbacks"}]})
        if method == "POST":
            assert self.columns is None, "Retry must discover the already-created table"
            self.columns = kwargs["json"]["tables"][0]["columns"]
        elif method == "GET" and url.endswith("/columns"):
            return Response({"columns": self.columns})
        elif method == "PUT":
            self.put_count += 1
            assert kwargs["params"] == {"noparse": "true"}
            for record in kwargs["json"]["records"]:
                assert not set(HUMAN_COLUMNS).intersection(record["fields"])
                key = (record["require"]["environment"], record["require"]["feedback_id"])
                if key not in self.rows:
                    self.rows[key] = {column: False for column in HUMAN_COLUMNS}
                self.rows[key].update(record["fields"])
            if self.fail_after == self.put_count:
                self.fail_after = None
                raise requests.Timeout("sensitive payload must never appear")
        else:
            raise AssertionError((method, url))
        if self.fail_after == method:
            self.fail_after = None
            raise requests.Timeout("sensitive payload must never appear")
        return Response()


@pytest.fixture
def grist(monkeypatch):
    server = GristServer()
    monkeypatch.setattr(requests, "request", server.request)
    config = FeedbackGristConfig("https://grist.example.test", "doc-id", "Feedbacks", "secret-key")
    return server, FeedbackGristClient(config)


def test_mapping_preserves_full_content_and_dashboard_conventions():
    records = build_feedback_records(feedbacks(), "production")
    first = records[0]
    assert first["require"] == {"environment": "production", "feedback_id": "42"}
    fields = first["fields"]
    assert fields["turn_id"] == "turn-1"
    assert fields["feedback_ts"] == "2026-09-15T08:00:00+00:00"
    assert fields["answer"] == "Réponse complète. " * 1000
    assert fields["stars"] == 1
    assert fields["helpful"] == "N"
    assert fields["reasons_positive"] == "claire;rapide"
    assert fields["reasons_negative"] == "incomplète"
    assert fields["comment"] == "À revoir"
    assert fields["beta_scope"] == "Non"
    assert fields["error_category"] == "missing_document"
    assert fields["ai_reason"] == "Hypothèse automatique"
    assert fields["rag_version"] == "v3"
    assert fields["dist_after_rerank"] == '{"MI": 2}'
    assert fields["ministry"] == "MI"
    assert not set(HUMAN_COLUMNS).intersection(fields)


def test_missing_run_analysis_and_nullable_values_are_exportable():
    data = pd.DataFrame([{"id": 9, "turn_id": pd.NA, "ai_reason": pd.NA, "ts": pd.NaT, "stars": float("nan")}])
    fields = build_feedback_records(data, "staging")[0]["fields"]
    assert fields["turn_id"] == fields["ai_reason"] == fields["feedback_ts"] == fields["helpful"] == ""
    assert fields["stars"] is None
    assert fields["ministry"] == "Non renseigné"


def test_multiple_feedbacks_for_same_turn_are_not_collapsed():
    data = pd.DataFrame([{"id": 1, "turn_id": "same"}, {"id": 2, "turn_id": "same"}])
    assert len(build_feedback_records(data, "staging")) == 2


def test_identical_join_duplicates_collapse_but_conflicting_rows_fail_before_network(grist):
    server, client = grist
    assert len(build_feedback_records(pd.concat([feedbacks(), feedbacks()]), "staging")) == 2
    bad = pd.DataFrame([{"id": 1, "answer": "A"}, {"id": 1, "answer": "B"}])
    with pytest.raises(FeedbackGristError, match="jointure"):
        client.sync(bad, "staging")
    assert not server.calls


@pytest.mark.parametrize("identifier", [None, pd.NA, float("nan"), "", True])
def test_missing_identity_fails_before_network(grist, identifier):
    server, client = grist
    with pytest.raises(FeedbackGristError, match="identifiant"):
        client.sync(pd.DataFrame([{"id": identifier}]), "staging")
    assert not server.calls


@pytest.mark.parametrize("annotations", list(product((True, False), repeat=3)))
def test_repeated_sync_preserves_checked_and_unchecked_human_annotations(grist, annotations):
    server, client = grist
    assert client.sync(feedbacks(), "production").synced == 2
    first = server.rows[("production", "42")]
    assert all(first[column] is False for column in HUMAN_COLUMNS)
    human = dict(zip(HUMAN_COLUMNS, annotations))
    first.update(human)
    changed = feedbacks()
    changed.loc[0, "comment"] = "Mis à jour"
    assert client.sync(changed, "production").error is None
    assert len(server.rows) == 2
    assert first["comment"] == "Mis à jour"
    assert {column: first[column] for column in HUMAN_COLUMNS} == human
    # Explicit manual unchecking must survive just as checking does.
    first.update({column: not value for column, value in human.items()})
    client.sync(changed, "production")
    assert {column: first[column] for column in HUMAN_COLUMNS} == {column: not value for column, value in human.items()}


def test_same_id_in_two_environments_stays_separate(grist):
    server, client = grist
    client.sync(feedbacks(), "staging")
    client.sync(feedbacks(), "production")
    assert len(server.rows) == 4


def test_partial_write_timeout_then_retry_does_not_duplicate_or_reset_annotations(grist):
    server, client = grist
    server.fail_after = 2
    result = client.sync(feedbacks(), "staging", batch_size=1)
    assert result.synced == 1 and result.total == 2 and result.error
    assert "sensitive" not in result.error
    assert len(server.rows) == 2  # failed response, but the write committed
    server.rows[("staging", "43")]["traite"] = True
    retry = client.sync(feedbacks(), "staging", batch_size=1)
    assert retry.synced == 2 and retry.error is None
    assert len(server.rows) == 2
    assert server.rows[("staging", "43")]["traite"] is True


def test_table_creation_timeout_is_discovered_on_retry(grist):
    server, client = grist
    server.fail_after = "POST"
    assert client.sync(feedbacks(), "staging").error
    assert client.sync(feedbacks(), "staging").synced == 2
    assert sum(method == "POST" for method, _, _ in server.calls) == 1


def test_auth_error_reports_zero_then_can_be_retried(grist):
    server, client = grist
    server.fail_before = "GET"
    result = client.sync(feedbacks(), "staging")
    assert result.synced == 0 and "403" in result.error
    assert "secret-key" not in result.error
    assert client.sync(feedbacks(), "staging").error is None


@pytest.mark.parametrize("change", [{"type": "Text"}, {"isFormula": True}, {"formula": "True"}])
def test_incompatible_annotation_schema_is_rejected_without_writes(grist, change):
    server, client = grist
    server.columns = feedback_columns()
    next(column for column in server.columns if column["id"] == "traite")["fields"].update(change)
    result = client.sync(feedbacks(), "staging")
    assert result.synced == 0 and "traite" in result.error
    assert not server.rows


def test_table_schema_creates_editable_bool_columns(grist):
    server, client = grist
    client.sync(feedbacks(), "staging")
    for column in server.columns:
        if column["id"] in HUMAN_COLUMNS:
            assert column["fields"] == {"label": HUMAN_COLUMNS[column["id"]], "type": "Bool", "isFormula": False, "formula": ""}


@pytest.mark.parametrize("payload", [{}, {"tables": None}, {"tables": [None]}, []])
def test_invalid_table_response_does_not_trigger_creation(grist, monkeypatch, payload):
    server, client = grist
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: payload)
    assert client.sync(feedbacks(), "staging").error
    assert not server.rows


def test_empty_selection_does_not_touch_grist(grist):
    server, client = grist
    assert client.sync(pd.DataFrame(), "staging").synced == 0
    assert not server.calls


def test_destination_is_explicit_and_separate_from_manifest(monkeypatch):
    for name, value in {
        "GRIST_API_BASE_URL": "https://grist.example.test",
        "GRIST_API_KEY": "secret-key",
        "GRIST_DOC_ID": "manifest",
        "GRIST_TABLE_ID": "Sources",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GRIST_FEEDBACK_DOC_ID", raising=False)
    monkeypatch.delenv("GRIST_FEEDBACK_TABLE_ID", raising=False)
    with pytest.raises(FeedbackGristError, match="GRIST_FEEDBACK_DOC_ID"):
        FeedbackGristConfig.from_env()
    monkeypatch.setenv("GRIST_FEEDBACK_DOC_ID", "manifest")
    monkeypatch.setenv("GRIST_FEEDBACK_TABLE_ID", "Sources")
    with pytest.raises(FeedbackGristError, match="dédiée"):
        FeedbackGristConfig.from_env()
    monkeypatch.setenv("GRIST_FEEDBACK_TABLE_ID", "Feedbacks")
    assert "secret-key" not in repr(FeedbackGristConfig.from_env())


def test_source_environment_uses_database_environment_over_local_app(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("APP_SCALEWAY_ENV", "production")
    assert feedback_source_environment() == "production"
    monkeypatch.delenv("APP_SCALEWAY_ENV")
    monkeypatch.delenv("APP_ENV")
    with pytest.raises(FeedbackGristError, match="APP_SCALEWAY_ENV"):
        feedback_source_environment()
