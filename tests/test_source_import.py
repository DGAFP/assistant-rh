"""Tests de la logique d'import de sources (src/ui/source_import.py) — issue #249."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ui.source_import import (
    DropzoneUploader,
    SourceImportError,
    build_cle_bucket,
    build_new_pdf_row,
    build_text_id_row,
    build_uid_from_bytes,
    build_uid_from_text_id,
    classify_text_id,
    content_identity,
    find_row_by_hash,
    find_row_by_record_id,
    find_row_by_uid,
    rows_missing_cle_bucket,
    sanitize_filename,
    validate_pdf_bytes,
)

# --- uid -----------------------------------------------------------------


def test_uid_from_bytes_is_deterministic_and_referential_shaped() -> None:
    uid = build_uid_from_bytes(b"%PDF-fake")
    assert uid == build_uid_from_bytes(b"%PDF-fake")
    assert uid != build_uid_from_bytes(b"%PDF-autre")
    assert len(uid) == 10
    assert all(c in "0123456789abcdef" for c in uid)


def test_uid_from_text_id_normalizes_case_and_spaces() -> None:
    assert build_uid_from_text_id(" legiarti000006900846 ") == build_uid_from_text_id("LEGIARTI000006900846")


def test_content_identity_returns_uid_prefix_of_full_hash() -> None:
    uid, full_hash = content_identity(b"%PDF-fake")
    assert uid == full_hash[:10]
    assert len(full_hash) == 64
    assert uid == build_uid_from_bytes(b"%PDF-fake")


def test_validate_pdf_bytes_enforces_magic_bytes() -> None:
    validate_pdf_bytes(b"%PDF-1.4 contenu")
    with pytest.raises(SourceImportError, match="Fichier vide"):
        validate_pdf_bytes(b"")
    with pytest.raises(SourceImportError, match="signature"):
        validate_pdf_bytes(b"<html>pas un pdf</html>")


# --- classification des ids ------------------------------------------------


@pytest.mark.parametrize(
    ("text_id", "expected_type"),
    [
        ("LEGIARTI000006900846", "legifrance_article"),
        ("legitext000044416551", "legifrance_code"),
        ("JORFTEXT000047304786", "jorf"),
        ("F12345", "service_public_fiche"),
        ("f1", "service_public_fiche"),
    ],
)
def test_classify_text_id_recognizes_known_formats(text_id: str, expected_type: str) -> None:
    assert classify_text_id(text_id) == expected_type


@pytest.mark.parametrize("bad_id", ["LEGIARTI123", "F1234567", "CIRCULAIRE-2024", "", "LEGIARTI00000690084X"])
def test_classify_text_id_rejects_unknown_formats(bad_id: str) -> None:
    with pytest.raises(SourceImportError, match="non reconnu"):
        classify_text_id(bad_id)


# --- clés bucket -----------------------------------------------------------


def test_sanitize_filename_makes_s3_safe_ascii() -> None:
    assert sanitize_filename("Règlement intérieur (2024).pdf") == "Reglement-interieur-2024.pdf"
    assert sanitize_filename("../../etc/passwd") == "passwd.pdf"
    assert sanitize_filename("") == "document.pdf"


def test_sanitize_filename_caps_length_keeping_extension() -> None:
    long_name = "x" * 300 + ".pdf"
    result = sanitize_filename(long_name)
    assert len(result) <= 80
    assert result.endswith(".pdf")


def test_build_cle_bucket_prefixes_corpus_and_uid() -> None:
    key = build_cle_bucket("MI", "abc123def0", "Circulaire ARTT.pdf")
    assert key == "mi/abc123def0_Circulaire-ARTT.pdf"

    with pytest.raises(SourceImportError, match="Corpus PDF inconnu"):
        build_cle_bucket("RGRH", "abc123def0", "doc.pdf")


# --- construction des lignes Grist ------------------------------------------


def test_build_new_pdf_row_matches_manifest_contract() -> None:
    fields = build_new_pdf_row(
        corpus="mi",
        uid="abc123def0",
        titre="  Circulaire temps de travail  ",
        cle_bucket="mi/abc123def0_circ.pdf",
        sous_thematique="Temps de travail",
        date_publication="2024-01-15",
    )
    assert fields["source_corpus"] == "MI"
    assert fields["uid"] == "abc123def0"
    assert fields["titre_document"] == "Circulaire temps de travail"
    assert fields["cle_bucket"] == "mi/abc123def0_circ.pdf"
    assert fields["abroge"] == ""
    assert fields["sous_thematique"] == "Temps de travail"
    assert fields["date_publication"] == "2024-01-15"
    assert fields["fichier_origine"] == "import_ui"


def test_build_new_pdf_row_requires_titre() -> None:
    with pytest.raises(SourceImportError, match="Titre obligatoire"):
        build_new_pdf_row(corpus="mi", uid="abc123def0", titre="  ", cle_bucket="mi/x.pdf")


def test_build_text_id_row_routes_to_the_right_corpus() -> None:
    fields = build_text_id_row(text_id="legiarti000006900846", titre="Article L.1")
    assert fields["source_corpus"] == "Interministériel/Légifrance"
    assert fields["type_id"] == "legifrance_article"
    assert fields["id_extraction"] == "LEGIARTI000006900846"
    assert "cle_bucket" not in fields

    fiche = build_text_id_row(text_id="F12345", titre="Fiche congés")
    assert fiche["source_corpus"] == "Service-public"
    assert fiche["type_id"] == "service_public_fiche"


# --- doublons / lignes en attente --------------------------------------------


def make_record(record_id: int, **fields: Any) -> dict[str, Any]:
    return {"id": record_id, "fields": fields}


def test_find_row_by_uid_is_case_insensitive() -> None:
    records = [make_record(1, uid="ABC123DEF0"), make_record(2, uid="fff")]
    assert find_row_by_uid(records, "abc123def0")["id"] == 1
    assert find_row_by_uid(records, "inconnu") is None


def test_find_row_by_hash_matches_hash_contenu_and_ignores_empty() -> None:
    records = [make_record(1, hash_contenu="A" * 64), make_record(2, hash_contenu="")]
    assert find_row_by_hash(records, "a" * 64)["id"] == 1
    assert find_row_by_hash(records, "b" * 64) is None
    # Un hash vide ne doit jamais matcher les lignes sans hash_contenu.
    assert find_row_by_hash(records, "") is None


def test_find_row_by_record_id() -> None:
    records = [make_record(7, uid="x"), make_record(9, uid="y")]
    assert find_row_by_record_id(records, 9)["fields"]["uid"] == "y"
    assert find_row_by_record_id(records, 1) is None


def test_rows_missing_cle_bucket_filters_corpus_and_emptiness() -> None:
    records = [
        make_record(1, source_corpus="MI", cle_bucket=""),
        make_record(2, source_corpus="MI", cle_bucket="mi/deja-la.pdf"),
        make_record(3, source_corpus="MASA", cle_bucket=""),
        make_record(4, source_corpus="mi", cle_bucket="   "),
    ]
    pending = rows_missing_cle_bucket(records, "MI")
    assert [record["id"] for record in pending] == [1, 4]


# --- uploader ----------------------------------------------------------------


def test_dropzone_uploader_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCW_ACCESS_KEY", raising=False)
    monkeypatch.delenv("SCW_SECRET_KEY", raising=False)
    with pytest.raises(SourceImportError, match="SCW_ACCESS_KEY"):
        DropzoneUploader.from_env()


def test_dropzone_uploader_puts_pdf_with_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeS3:
        def put_object(self, **kwargs: Any) -> None:
            calls.update(kwargs)

    uploader = DropzoneUploader(bucket="assistant-rh-sources-pdf", region="fr-par", access_key="ak", secret_key="sk")
    monkeypatch.setattr(uploader, "_client", lambda: FakeS3())

    uri = uploader.upload_pdf("mi/abc_doc.pdf", b"%PDF-fake")

    assert uri == "s3://assistant-rh-sources-pdf/mi/abc_doc.pdf"
    assert calls == {
        "Bucket": "assistant-rh-sources-pdf",
        "Key": "mi/abc_doc.pdf",
        "Body": b"%PDF-fake",
        "ContentType": "application/pdf",
    }


def test_dropzone_uploader_wraps_s3_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingS3:
        def put_object(self, **kwargs: Any) -> None:
            raise RuntimeError("An error occurred (403) when calling the PutObject operation")

    uploader = DropzoneUploader(bucket="b", region="fr-par", access_key="ak", secret_key="sk")
    monkeypatch.setattr(uploader, "_client", lambda: FailingS3())

    # Pas de traceback boto3 brut dans l'UI: erreur métier explicite.
    with pytest.raises(SourceImportError, match="Échec de l'upload dropzone"):
        uploader.upload_pdf("mi/x.pdf", b"%PDF-fake")
