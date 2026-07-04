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
    content_type_for,
    find_row_by_hash,
    find_row_by_record_id,
    find_row_by_uid,
    plan_attach_pdf_import,
    plan_new_pdf_import,
    rows_missing_cle_bucket,
    sanitize_filename,
    validate_source_bytes,
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


def test_build_cle_bucket_prefixes_corpus_and_uid() -> None:
    key = build_cle_bucket("MI", "abc123def0", "Circulaire ARTT.pdf")
    assert key == "mi/abc123def0_Circulaire-ARTT.pdf"

    with pytest.raises(SourceImportError, match="Corpus PDF inconnu"):
        build_cle_bucket("RGRH", "abc123def0", "doc.pdf")


# --- formats multi-sources (pdf, doc, docx, xls, xlsx) -----------------------


def test_sanitize_filename_preserves_supported_extensions() -> None:
    assert sanitize_filename("Barème indemnités (2024).xlsx") == "Bareme-indemnites-2024.xlsx"
    assert sanitize_filename("note de service.doc") == "note-de-service.doc"
    assert build_cle_bucket("MI", "abc123def0", "tableau.xlsx") == "mi/abc123def0_tableau.xlsx"


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("doc.pdf", b"%PDF-1.7 x"),
        ("doc.docx", b"PK\x03\x04reste-du-zip"),
        ("doc.xlsx", b"PK\x03\x04reste-du-zip"),
        ("doc.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1ole2"),
        ("doc.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1ole2"),
    ],
)
def test_validate_source_bytes_accepts_matching_signatures(filename: str, data: bytes) -> None:
    validate_source_bytes(filename, data)


@pytest.mark.parametrize(
    ("filename", "data", "expected_error"),
    [
        ("doc.pdf", b"PK\x03\x04zip-pas-pdf", "signature"),
        ("doc.xlsx", b"%PDF-1.7", "signature"),
        ("doc.txt", b"peu importe", "Format non supporté"),
        ("doc.pdf", b"", "Fichier vide"),
    ],
)
def test_validate_source_bytes_rejects_mismatches(filename: str, data: bytes, expected_error: str) -> None:
    with pytest.raises(SourceImportError, match=expected_error):
        validate_source_bytes(filename, data)


def test_content_type_for_follows_extension() -> None:
    assert content_type_for("mi/abc_doc.pdf") == "application/pdf"
    assert content_type_for("mi/abc_tableau.xlsx").endswith("spreadsheetml.sheet")
    assert content_type_for("mi/abc_note.doc") == "application/msword"


def test_plan_accepts_xlsx_source() -> None:
    plan = plan_new_pdf_import(
        records=[],
        corpus="MI",
        filename="Barème.xlsx",
        pdf_bytes=b"PK\x03\x04contenu",
        titre="Barème indemnités",
    )
    assert plan.cle_bucket.endswith(".xlsx")


def test_dropzone_uploader_sets_content_type_from_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeS3:
        def put_object(self, **kwargs: Any) -> None:
            calls.update(kwargs)

    uploader = DropzoneUploader(bucket="b", region="fr-par", access_key="ak", secret_key="sk")
    monkeypatch.setattr(uploader, "_client", lambda: FakeS3())

    uploader.upload_file("mi/abc_tableau.xlsx", b"PK\x03\x04contenu")
    assert calls["ContentType"].endswith("spreadsheetml.sheet")


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


def test_rows_missing_cle_bucket_filters_corpus_and_emptiness() -> None:
    records = [
        make_record(1, source_corpus="MI", cle_bucket=""),
        make_record(2, source_corpus="MI", cle_bucket="mi/deja-la.pdf"),
        make_record(3, source_corpus="MASA", cle_bucket=""),
        make_record(4, source_corpus="mi", cle_bucket="   "),
    ]
    pending = rows_missing_cle_bucket(records, "MI")
    assert [record["id"] for record in pending] == [1, 4]


# --- plans d'import PDF -------------------------------------------------------


def test_plan_new_pdf_import_validates_row_before_upload_step() -> None:
    with pytest.raises(SourceImportError, match="Titre obligatoire"):
        plan_new_pdf_import(
            records=[],
            corpus="MI",
            filename="doc.pdf",
            pdf_bytes=b"%PDF-fake",
            titre=" ",
        )


def test_plan_new_pdf_import_rejects_duplicate_content_uid() -> None:
    pdf_bytes = b"%PDF-fake"
    duplicate = make_record(7, uid=build_uid_from_bytes(pdf_bytes), titre_document="Deja la")

    with pytest.raises(SourceImportError, match="existe déjà"):
        plan_new_pdf_import(
            records=[duplicate],
            corpus="MI",
            filename="doc.pdf",
            pdf_bytes=pdf_bytes,
            titre="Document",
        )


def test_plan_attach_pdf_import_uses_content_uid_and_rejects_other_row_duplicate() -> None:
    pdf_bytes = b"%PDF-fake"
    content_uid = build_uid_from_bytes(pdf_bytes)
    selected = make_record(1, uid="ancienuid", titre_document="A completer")
    duplicate = make_record(2, uid=content_uid, titre_document="Deja la")

    with pytest.raises(SourceImportError, match="record 2"):
        plan_attach_pdf_import(records=[selected, duplicate], selected_row=selected, corpus="MI", filename="doc.pdf", pdf_bytes=pdf_bytes)

    plan = plan_attach_pdf_import(records=[selected], selected_row=selected, corpus="MI", filename="doc.pdf", pdf_bytes=pdf_bytes)
    assert plan.record_id == 1
    assert plan.fields == {
        "uid": content_uid,
        "cle_bucket": f"mi/{content_uid}_doc.pdf",
        "hash_contenu": content_identity(pdf_bytes)[1],
    }


def test_plan_attach_pdf_import_refuses_row_already_filled() -> None:
    selected = make_record(1, uid="abc", cle_bucket="mi/deja-la.pdf")
    with pytest.raises(SourceImportError, match="a déjà un PDF"):
        plan_attach_pdf_import(records=[selected], selected_row=selected, corpus="MI", filename="doc.pdf", pdf_bytes=b"%PDF-fake")


def test_plan_rejects_non_pdf_content() -> None:
    with pytest.raises(SourceImportError, match="signature"):
        plan_new_pdf_import(records=[], corpus="MI", filename="doc.pdf", pdf_bytes=b"<html>", titre="Doc")


def test_plan_detects_duplicate_by_hash_contenu() -> None:
    pdf_bytes = b"%PDF-fake"
    _, full_hash = content_identity(pdf_bytes)
    # Ligne avec un uid différent mais le même hash_contenu (déjà uploadée).
    duplicate = make_record(3, uid="autre-uid", hash_contenu=full_hash, titre_document="Deja la")

    with pytest.raises(SourceImportError, match="record 3"):
        plan_new_pdf_import(records=[duplicate], corpus="MI", filename="doc.pdf", pdf_bytes=pdf_bytes, titre="Doc")


def test_content_identity_returns_uid_prefix_of_full_hash() -> None:
    uid, full_hash = content_identity(b"%PDF-fake")
    assert uid == full_hash[:10]
    assert len(full_hash) == 64
    assert uid == build_uid_from_bytes(b"%PDF-fake")


def test_sanitize_filename_caps_length_keeping_extension() -> None:
    result = sanitize_filename("x" * 300 + ".pdf")
    assert len(result) <= 80
    assert result.endswith(".pdf")


def test_find_row_by_hash_matches_hash_contenu_and_ignores_empty() -> None:
    records = [make_record(1, hash_contenu="A" * 64), make_record(2, hash_contenu="")]
    assert find_row_by_hash(records, "a" * 64)["id"] == 1
    assert find_row_by_hash(records, "b" * 64) is None
    assert find_row_by_hash(records, "") is None


def test_find_row_by_record_id() -> None:
    records = [make_record(7, uid="x"), make_record(9, uid="y")]
    assert find_row_by_record_id(records, 9)["fields"]["uid"] == "y"
    assert find_row_by_record_id(records, 1) is None


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


def test_dropzone_uploader_wraps_upload_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingS3:
        def put_object(self, **_kwargs: Any) -> None:
            raise RuntimeError("denied")

    uploader = DropzoneUploader(bucket="assistant-rh-sources-pdf", region="fr-par", access_key="ak", secret_key="sk")
    monkeypatch.setattr(uploader, "_client", lambda: FailingS3())

    with pytest.raises(SourceImportError, match="Upload dropzone impossible"):
        uploader.upload_pdf("mi/abc_doc.pdf", b"%PDF-fake")


def test_fetch_file_reads_dropzone_object(monkeypatch):
    from src.ui.source_import import DropzoneUploader

    uploader = DropzoneUploader(bucket="b", region="fr-par", access_key="ak", secret_key="sk")

    class FakeBody:
        def read(self) -> bytes:
            return b"%PDF-contenu"

    class FakeClient:
        def get_object(self, Bucket: str, Key: str):
            assert Bucket == "b"
            assert Key == "mi/abc_notice.pdf"
            return {"Body": FakeBody()}

    monkeypatch.setattr(DropzoneUploader, "_client", lambda self: FakeClient())
    assert uploader.fetch_file("mi/abc_notice.pdf") == b"%PDF-contenu"


def test_fetch_file_wraps_errors(monkeypatch):
    import pytest

    from src.ui.source_import import DropzoneUploader, SourceImportError

    uploader = DropzoneUploader(bucket="b", region="fr-par", access_key="ak", secret_key="sk")

    class FakeClient:
        def get_object(self, Bucket: str, Key: str):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(DropzoneUploader, "_client", lambda self: FakeClient())
    with pytest.raises(SourceImportError, match="mi/introuvable.pdf"):
        uploader.fetch_file("mi/introuvable.pdf")


def test_is_dropzone_key_accepts_corpus_keys_only():
    from src.ui.source_import import is_dropzone_key

    assert is_dropzone_key("mi/3a6a62f289_notice.pdf") is True
    assert is_dropzone_key("MASA/abc_note.docx") is True
    # Chemins bruts d'autres sources (Légifrance): jamais de GET dropzone.
    assert is_dropzone_key("/data/raw/legifrance/texte.txt") is False
    assert is_dropzone_key("s3://autre-bucket/cle.json") is False
    assert is_dropzone_key("data/lake/legifrance/bronze/x.json") is False
    # Clés malformées.
    assert is_dropzone_key("mi/") is False
    assert is_dropzone_key("mi") is False
    assert is_dropzone_key("") is False
    assert is_dropzone_key(None) is False
