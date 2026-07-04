"""Tests du client Grist et du contrat de manifest (utils/grist.py).

HTTP mocké — aucun appel réseau. Le contrat: colonne manquante => échec franc
(GristContractError), ligne invalide => rejet tracé, le run continue.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from assistant_rh_data_engineering.utils import grist as grist_module
from assistant_rh_data_engineering.utils.grist import (
    GristClient,
    GristConfig,
    GristContractError,
    GristError,
    ManifestRow,
    fetch_validated_manifest,
    validate_manifest_columns,
    validate_manifest_records,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def make_client(table_id: str | None = "Manifest") -> GristClient:
    config = GristConfig(
        base_url="https://grist.example",
        api_key="secret",
        doc_id="doc123",
        table_id=table_id,
    )
    return GristClient(config)


# --- Config ---------------------------------------------------------------


def test_config_from_env_fails_with_explicit_missing_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GRIST_API_BASE_URL", "GRIST_API_KEY", "GRIST_DOC_ID", "GRIST_TABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRIST_API_KEY", "k")

    with pytest.raises(GristError, match="GRIST_API_BASE_URL, GRIST_DOC_ID"):
        GristConfig.from_env()


def test_config_from_env_reads_optional_table_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIST_API_BASE_URL", "https://grist.example/")
    monkeypatch.setenv("GRIST_API_KEY", "k")
    monkeypatch.setenv("GRIST_DOC_ID", "doc123")
    monkeypatch.setenv("GRIST_TABLE_ID", "Manifest")

    config = GristConfig.from_env()

    assert config.base_url == "https://grist.example"
    assert config.table_id == "Manifest"


# --- Client HTTP ----------------------------------------------------------


def test_list_records_hits_records_endpoint_with_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, headers: dict, params: dict | None, timeout: int) -> FakeResponse:
        seen.update({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return FakeResponse({"records": [{"id": 1, "fields": {"titre": "Doc"}}]})

    monkeypatch.setattr(grist_module.requests, "get", fake_get)

    records = make_client().list_records(filter={"ministere": ["mi"]})

    assert records == [{"id": 1, "fields": {"titre": "Doc"}}]
    assert seen["url"] == "https://grist.example/api/docs/doc123/tables/Manifest/records"
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert json.loads(seen["params"]["filter"]) == {"ministere": ["mi"]}


def test_list_records_requires_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GristError, match="GRIST_TABLE_ID"):
        make_client(table_id=None).list_records()


def test_get_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grist_module.requests,
        "get",
        lambda url, headers, params, timeout: FakeResponse({"error": "forbidden"}, status_code=403),
    )

    with pytest.raises(GristError, match="HTTP 403"):
        make_client().list_columns()


def test_update_records_patches_with_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_patch(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        seen.update({"url": url, "body": json})
        return FakeResponse({})

    monkeypatch.setattr(grist_module.requests, "patch", fake_patch)

    make_client().writeback_status(7, {"statut_ingestion": "ok", "nb_chunks": 12})

    assert seen["url"].endswith("/tables/Manifest/records")
    assert seen["body"] == {"records": [{"id": 7, "fields": {"statut_ingestion": "ok", "nb_chunks": 12}}]}


def test_update_records_noop_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_patch(*args: object, **kwargs: object) -> None:
        raise AssertionError("PATCH ne doit pas être appelé pour une liste vide")

    monkeypatch.setattr(grist_module.requests, "patch", fail_patch)
    make_client().update_records([])


def test_add_records_returns_created_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        grist_module.requests,
        "post",
        lambda url, headers, json, timeout: FakeResponse({"records": [{"id": 41}, {"id": 42}]}),
    )

    ids = make_client().add_records([{"fields": {"titre": "a"}}, {"fields": {"titre": "b"}}])

    assert ids == [41, 42]


# --- Contrat de colonnes ---------------------------------------------------


def test_validate_manifest_columns_hard_fails_on_missing() -> None:
    # Colonnes du référentiel réel, sans cle_bucket (la colonne à ajouter).
    with pytest.raises(GristContractError, match="cle_bucket"):
        validate_manifest_columns(["source_corpus", "uid", "titre_document", "abroge"])


def test_fetch_validated_manifest_checks_columns_before_records(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr(client, "list_columns", lambda table_id=None: ["source_corpus"])

    def fail_list_records(*args: object, **kwargs: object) -> None:
        raise AssertionError("les records ne doivent pas être lus si le contrat de colonnes échoue")

    monkeypatch.setattr(client, "list_records", fail_list_records)

    with pytest.raises(GristContractError):
        fetch_validated_manifest(client, "mi")


# --- Validation des lignes ---------------------------------------------------


def manifest_fields(**overrides: Any) -> dict[str, Any]:
    # Schéma du référentiel réel (table multi-corpus, colonnes existantes
    # titre_document/source_corpus/uid/abroge + cle_bucket ajoutée).
    fields = {
        "source_corpus": "MI",
        "uid": "7361bf3024",
        "titre_document": "Circulaire temps de travail",
        "cle_bucket": "mi/circulaire-temps-travail.pdf",
        "abroge": "",
        "date_publication": "2024-01-15",
        "cle_matching": "colonne héritée ignorée",
    }
    fields.update(overrides)
    return fields


def test_validate_manifest_records_accepts_valid_row_case_insensitive() -> None:
    records = [{"id": 1, "fields": manifest_fields()}]

    result = validate_manifest_records(records, "mi")

    assert result.ok
    row = result.valid[0]
    assert isinstance(row, ManifestRow)
    assert row.short_id == "7361BF3024"
    assert row.corpus == "MI"
    assert row.statut == "en_vigueur"
    assert row.date_publication == "2024-01-15"
    assert row.record_id == 1


def test_validate_manifest_records_maps_abroge_oui_to_abroge() -> None:
    records = [{"id": 1, "fields": manifest_fields(abroge="Oui")}]

    result = validate_manifest_records(records, "mi")

    assert result.valid[0].statut == "abroge"


def test_validate_manifest_records_ignores_other_corpora_without_rejecting() -> None:
    records = [
        {"id": 1, "fields": manifest_fields()},
        {"id": 2, "fields": manifest_fields(source_corpus="MASA", uid="aaaa000001")},
        {"id": 3, "fields": manifest_fields(source_corpus="Interministériel/Légifrance", uid="bbbb000002")},
    ]

    result = validate_manifest_records(records, "mi")

    assert len(result.valid) == 1
    assert result.rejected == []


def test_validate_manifest_records_tolerates_missing_date_publication() -> None:
    records = [{"id": 1, "fields": manifest_fields(date_publication="")}]

    result = validate_manifest_records(records, "mi")

    assert result.ok
    assert result.valid[0].date_publication is None


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"uid": " "}, "uid vide"),
        ({"titre_document": "  "}, "titre_document vide"),
        ({"cle_bucket": ""}, "cle_bucket vide"),
        ({"abroge": "peut-être"}, "abroge invalide"),
    ],
)
def test_validate_manifest_records_rejects_invalid_rows(overrides: dict[str, Any], expected_error: str) -> None:
    records = [{"id": 5, "fields": manifest_fields(**overrides)}]

    result = validate_manifest_records(records, "mi")

    assert result.valid == []
    assert len(result.rejected) == 1
    assert any(expected_error in error for error in result.rejected[0].errors)


def test_validate_manifest_records_rejects_duplicate_uid_but_keeps_first() -> None:
    records = [
        {"id": 1, "fields": manifest_fields()},
        {"id": 2, "fields": manifest_fields(uid="7361BF3024", titre_document="Autre titre")},
    ]

    result = validate_manifest_records(records, "mi")

    assert [row.record_id for row in result.valid] == [1]
    assert result.rejected[0].record_id == 2
    assert any("doublon" in error for error in result.rejected[0].errors)


def test_statut_ingestion_inactif_rend_la_ligne_abrogee() -> None:
    # Colonne de statut unique: a_supprimer (opérateur) et supprime (job)
    # rendent la ligne inactive comme le drapeau juridique abroge.
    from assistant_rh_data_engineering.utils.grist import validate_manifest_records

    def record(record_id: int, statut_ingestion: str) -> dict:
        return {
            "id": record_id,
            "fields": {
                "source_corpus": "MI",
                "uid": f"uid-{record_id}",
                "titre_document": "Titre",
                "cle_bucket": f"mi/uid-{record_id}_doc.pdf",
                "abroge": "",
                "statut_ingestion": statut_ingestion,
            },
        }

    validation = validate_manifest_records(
        [record(1, "a_supprimer"), record(2, "supprime"), record(3, "ok"), record(4, "")],
        "MI",
    )

    statuts = {row.uid: row.statut for row in validation.valid}
    assert statuts == {
        "uid-1": "abroge",
        "uid-2": "abroge",
        "uid-3": "en_vigueur",
        "uid-4": "en_vigueur",
    }
    assert validation.rejected == []
