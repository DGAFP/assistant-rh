"""Logique métier de la page d'import de sources (issue #249).

Séparée de la page Streamlit pour être testable sans UI: validation des ids
Légifrance/Service-Public, génération d'uid, construction des lignes Grist,
upload S3 (boto3 — pas d'aws CLI dans l'image Streamlit, contrairement aux
jobs data-engineering qui font du sync massif).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from assistant_rh_data_engineering.utils.helpers import sha256_bytes

# Corpus PDF ministériels (lignes du référentiel avec cle_bucket + dropzone).
PDF_CORPORA: tuple[str, ...] = ("MI", "MASA", "MATTE", "MSO")

# Corpus à ajout unitaire par identifiant (pas de PDF, consommés par les
# pipelines Légifrance / Service-Public existants — Phase E, #249).
TEXT_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "legifrance_article": re.compile(r"^LEGIARTI\d{12}$"),
    "legifrance_code": re.compile(r"^LEGITEXT\d{12}$"),
    "jorf": re.compile(r"^JORFTEXT\d{12}$"),
    "service_public_fiche": re.compile(r"^F\d{1,6}$"),
}

TEXT_ID_CORPUS: dict[str, str] = {
    "legifrance_article": "Interministériel/Légifrance",
    "legifrance_code": "Interministériel/Légifrance",
    "jorf": "Interministériel/Légifrance",
    "service_public_fiche": "Service-public",
}

# Longueur des uid du référentiel existant (hex, ex: 7361bf3024).
_UID_LENGTH = 10

# Cap du nom de fichier dans la clé S3 (clé totale ~ corpus + uid + nom).
_MAX_FILENAME_LENGTH = 80

# Signatures de fichiers OLE2 (doc/xls historiques) et ZIP (docx/xlsx).
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_SIGNATURE = b"PK\x03\x04"

# Formats acceptés dans la dropzone. Les non-PDF sont convertis en PDF par le
# bronze du pipeline (LibreOffice headless) avant OCR — décision 2026-07-04,
# flux .doc/.xlsx récurrent dans les sources ministérielles.
SUPPORTED_SOURCE_FORMATS: dict[str, dict[str, Any]] = {
    ".pdf": {"signatures": (b"%PDF-",), "content_type": "application/pdf"},
    ".doc": {"signatures": (_OLE2_SIGNATURE,), "content_type": "application/msword"},
    ".xls": {"signatures": (_OLE2_SIGNATURE,), "content_type": "application/vnd.ms-excel"},
    ".docx": {
        "signatures": (_ZIP_SIGNATURE,),
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    ".xlsx": {
        "signatures": (_ZIP_SIGNATURE,),
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
}


class SourceImportError(ValueError):
    """Entrée invalide dans le formulaire d'import."""


def source_extension(filename: str) -> str:
    """Extension supportée du fichier source, en minuscules."""
    extension = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    if extension not in SUPPORTED_SOURCE_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_FORMATS))
        raise SourceImportError(f"Format non supporté: {filename!r} (attendus: {supported})")
    return extension


def validate_source_bytes(filename: str, data: bytes) -> None:
    """Contrôle serveur du contenu: le filtre type= de st.file_uploader est
    purement côté client. Vérifie la signature attendue pour l'extension."""
    if not data:
        raise SourceImportError("Fichier vide.")
    extension = source_extension(filename)
    signatures = SUPPORTED_SOURCE_FORMATS[extension]["signatures"]
    if not any(data.startswith(signature) for signature in signatures):
        raise SourceImportError(f"Le contenu ne correspond pas au format {extension} (signature invalide).")


def content_type_for(filename: str) -> str:
    return str(SUPPORTED_SOURCE_FORMATS[source_extension(filename)]["content_type"])


@dataclass(frozen=True)
class PdfImportPlan:
    """Action Grist à appliquer après un upload PDF réussi."""

    cle_bucket: str
    fields: dict[str, Any]
    record_id: int | None = None


def build_uid_from_bytes(pdf_bytes: bytes) -> str:
    """uid déterministe depuis le contenu: même PDF => même uid (idempotent)."""
    return sha256_bytes(pdf_bytes)[:_UID_LENGTH]


def content_identity(pdf_bytes: bytes) -> tuple[str, str]:
    """(uid, hash_contenu) du PDF: uid court style référentiel + sha256 complet
    pour la détection de doublon par contenu et le writeback."""
    full_hash = sha256_bytes(pdf_bytes)
    return full_hash[:_UID_LENGTH], full_hash


def build_uid_from_text_id(text_id: str) -> str:
    """uid déterministe pour un ajout unitaire Légifrance/SP."""
    return sha256_bytes(text_id.strip().upper().encode("utf-8"))[:_UID_LENGTH]


def classify_text_id(text_id: str) -> str:
    """Retourne le type_id du référentiel pour un identifiant Légifrance/SP.

    Échec explicite si le format n'est reconnu par aucun pattern.
    """
    normalized = text_id.strip().upper()
    for type_id, pattern in TEXT_ID_PATTERNS.items():
        if pattern.match(normalized):
            return type_id
    raise SourceImportError(f"Identifiant non reconnu: {text_id!r} (attendus: LEGIARTI/LEGITEXT/JORFTEXT + 12 chiffres, ou Fxxxxx)")


def sanitize_filename(filename: str) -> str:
    """Nom de fichier sûr pour une clé S3: ascii, sans espaces ni chemins.

    L'extension d'origine (format supporté) est préservée; défaut .pdf pour
    une entrée vide/inconnue.
    """
    raw_base = os.path.basename(filename or "").strip()
    extension = os.path.splitext(raw_base)[1].lower()
    if extension not in SUPPORTED_SOURCE_FORMATS:
        extension = ".pdf"

    stem = os.path.splitext(raw_base)[0] or "document"
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    if not stem:
        stem = "document"
    max_stem = _MAX_FILENAME_LENGTH - len(extension)
    if len(stem) > max_stem:
        # Clé S3 bornée: on tronque le nom, pas l'extension.
        stem = stem[:max_stem].rstrip("-._")
    return f"{stem}{extension}"


def build_cle_bucket(corpus: str, uid: str, filename: str) -> str:
    """Clé dropzone: {corpus}/{uid}_{nom-fichier}.pdf (uid => pas de collision)."""
    if corpus.upper() not in PDF_CORPORA:
        raise SourceImportError(f"Corpus PDF inconnu: {corpus!r} (attendus: {', '.join(PDF_CORPORA)})")
    return f"{corpus.lower()}/{uid}_{sanitize_filename(filename)}"


def build_new_pdf_row(
    *,
    corpus: str,
    uid: str,
    titre: str,
    cle_bucket: str,
    sous_thematique: str = "",
    date_publication: str | None = None,
    hash_contenu: str = "",
) -> dict[str, Any]:
    """Ligne Grist pour un nouveau document PDF (contrat REQUIRED_MANIFEST_COLUMNS)."""
    titre = titre.strip()
    if not titre:
        raise SourceImportError("Titre obligatoire.")
    fields: dict[str, Any] = {
        "source_corpus": corpus.upper(),
        "uid": uid,
        "titre_document": titre,
        "cle_bucket": cle_bucket,
        "abroge": "",
        "fichier_origine": "import_ui",
    }
    if sous_thematique.strip():
        fields["sous_thematique"] = sous_thematique.strip()
    if date_publication:
        fields["date_publication"] = date_publication
    if hash_contenu:
        fields["hash_contenu"] = hash_contenu
    return fields


def build_text_id_row(
    *,
    text_id: str,
    titre: str,
    sous_thematique: str = "",
) -> dict[str, Any]:
    """Ligne Grist pour un ajout unitaire Légifrance/SP (id dans id_extraction,
    colonnes du référentiel existant)."""
    type_id = classify_text_id(text_id)
    titre = titre.strip()
    if not titre:
        raise SourceImportError("Titre obligatoire.")
    fields: dict[str, Any] = {
        "source_corpus": TEXT_ID_CORPUS[type_id],
        "uid": build_uid_from_text_id(text_id),
        "titre_document": titre,
        "id_extraction": text_id.strip().upper(),
        "type_id": type_id,
        "abroge": "",
        "fichier_origine": "import_ui",
    }
    if sous_thematique.strip():
        fields["sous_thematique"] = sous_thematique.strip()
    return fields


def find_row_by_uid(records: list[dict[str, Any]], uid: str) -> dict[str, Any] | None:
    """Détection de doublon: même uid déjà présent dans le référentiel."""
    for record in records:
        fields = record.get("fields") or {}
        if str(fields.get("uid") or "").strip().lower() == uid.strip().lower():
            return record
    return None


def _record_id(record: dict[str, Any]) -> int:
    return int(record.get("id") or 0)


def _duplicate_message(record: dict[str, Any]) -> str:
    fields = record.get("fields") or {}
    titre = str(fields.get("titre_document") or "(sans titre)")
    uid = str(fields.get("uid") or "?")
    return f"record {_record_id(record)} ({titre}, uid {uid})"


def find_row_by_hash(records: list[dict[str, Any]], hash_contenu: str) -> dict[str, Any] | None:
    """Détection de doublon par contenu: même hash_contenu (sha256 complet),
    écrit par la page à l'upload puis confirmé par le pipeline."""
    normalized = hash_contenu.strip().lower()
    if not normalized:
        return None
    for record in records:
        fields = record.get("fields") or {}
        if str(fields.get("hash_contenu") or "").strip().lower() == normalized:
            return record
    return None


def find_row_by_record_id(records: list[dict[str, Any]], record_id: int) -> dict[str, Any] | None:
    """Relocalise une ligne par id de record (revalidation au moment du submit)."""
    for record in records:
        if _record_id(record) == record_id:
            return record
    return None


def _find_content_duplicate(
    records: list[dict[str, Any]],
    content_uid: str,
    content_hash: str,
) -> dict[str, Any] | None:
    return find_row_by_uid(records, content_uid) or find_row_by_hash(records, content_hash)


def plan_attach_pdf_import(
    *,
    records: list[dict[str, Any]],
    selected_row: dict[str, Any] | None,
    corpus: str,
    filename: str,
    pdf_bytes: bytes,
) -> PdfImportPlan:
    """Prépare le PATCH Grist avant upload pour une ligne existante.

    Le uid de la ligne complétée devient le uid dérivé du contenu; cela rend
    la détection de doublon fiable pour les prochains uploads. À appeler avec
    des records FRAIS (relus au submit): l'état affiché peut être périmé.
    """
    validate_source_bytes(filename, pdf_bytes)
    if selected_row is None:
        raise SourceImportError("Sélectionner une ligne du référentiel.")
    selected_fields = selected_row.get("fields") or {}
    if str(selected_fields.get("cle_bucket") or "").strip():
        raise SourceImportError(f"La ligne a déjà un PDF ({selected_fields.get('cle_bucket')}) — recharger la page.")

    content_uid, content_hash = content_identity(pdf_bytes)
    duplicate = _find_content_duplicate(records, content_uid, content_hash)
    selected_id = _record_id(selected_row)
    if duplicate and _record_id(duplicate) != selected_id:
        raise SourceImportError(f"Ce PDF existe déjà dans le référentiel: {_duplicate_message(duplicate)}")

    cle_bucket = build_cle_bucket(corpus, content_uid, filename)
    return PdfImportPlan(
        record_id=selected_id,
        cle_bucket=cle_bucket,
        fields={"uid": content_uid, "cle_bucket": cle_bucket, "hash_contenu": content_hash},
    )


def plan_new_pdf_import(
    *,
    records: list[dict[str, Any]],
    corpus: str,
    filename: str,
    pdf_bytes: bytes,
    titre: str,
    sous_thematique: str = "",
    date_publication: str | None = None,
) -> PdfImportPlan:
    """Prépare la création Grist avant upload pour éviter un objet orphelin."""
    validate_source_bytes(filename, pdf_bytes)
    content_uid, content_hash = content_identity(pdf_bytes)
    duplicate = _find_content_duplicate(records, content_uid, content_hash)
    if duplicate:
        raise SourceImportError(f"Ce PDF existe déjà dans le référentiel: {_duplicate_message(duplicate)}")

    cle_bucket = build_cle_bucket(corpus, content_uid, filename)
    fields = build_new_pdf_row(
        corpus=corpus,
        uid=content_uid,
        titre=titre,
        cle_bucket=cle_bucket,
        sous_thematique=sous_thematique,
        date_publication=date_publication,
        hash_contenu=content_hash,
    )
    return PdfImportPlan(cle_bucket=cle_bucket, fields=fields)


def rows_missing_cle_bucket(records: list[dict[str, Any]], corpus: str) -> list[dict[str, Any]]:
    """Lignes du corpus sans cle_bucket: la liste de travail des PDF à déposer."""
    expected = corpus.strip().lower()
    result = []
    for record in records:
        fields = record.get("fields") or {}
        if str(fields.get("source_corpus") or "").strip().lower() != expected:
            continue
        if not str(fields.get("cle_bucket") or "").strip():
            result.append(record)
    return result


def is_dropzone_key(storage_path: str) -> bool:
    """Vrai si storage_path est une clé de la dropzone ({corpus}/{fichier}).

    Les documents des autres sources portent des chemins bruts dans
    storage_path (Légifrance: chemin local ou URI s3 du bronze) qui ne
    doivent jamais déclencher de lecture dropzone dans le viewer.
    """
    key = (storage_path or "").strip()
    if "/" not in key or key.endswith("/"):
        return False
    return key.split("/", 1)[0].upper() in PDF_CORPORA


@dataclass
class DropzoneUploader:
    """Accès unitaire à la dropzone via boto3 (S3-compatible Scaleway):
    upload et suppression pour la page d'import, lecture pour le viewer de
    sources.

    Les jobs data-engineering utilisent l'aws CLI (sync massif d'arbres);
    l'UI ne fait que des opérations unitaires: boto3 suffit et évite
    d'embarquer la CLI dans l'image Streamlit.
    """

    bucket: str
    region: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls) -> "DropzoneUploader":
        region = os.getenv("SCW_DEFAULT_REGION", "fr-par")
        access_key = os.getenv("SCW_ACCESS_KEY", "")
        secret_key = os.getenv("SCW_SECRET_KEY", "")
        if not access_key or not secret_key:
            raise SourceImportError("SCW_ACCESS_KEY / SCW_SECRET_KEY manquants pour l'upload dropzone.")
        bucket = os.getenv("SCW_BUCKET_SOURCES_PDF", "assistant-rh-sources-pdf")
        return cls(bucket=bucket, region=region, access_key=access_key, secret_key=secret_key)

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=f"https://s3.{self.region}.scw.cloud",
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def upload_file(self, key: str, data: bytes) -> str:
        """Upload d'un fichier source; ContentType dérivé de l'extension de la clé."""
        try:
            self._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type_for(key),
            )
        except SourceImportError:
            raise
        except Exception as exc:
            raise SourceImportError(f"Upload dropzone impossible pour {key}: {exc}") from exc
        return f"s3://{self.bucket}/{key}"

    # Compat: nom historique quand la page ne gérait que le PDF.
    upload_pdf = upload_file

    def fetch_file(self, key: str) -> bytes:
        """Lecture d'un fichier source (viewer de sources: storage_path des
        documents des corpus PDF = clé dropzone)."""
        try:
            response = self._client().get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:
            raise SourceImportError(f"Lecture dropzone impossible pour {key}: {exc}") from exc

    def delete_file(self, key: str) -> None:
        try:
            self._client().delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise SourceImportError(f"Suppression dropzone impossible pour {key}: {exc}") from exc

    delete_pdf = delete_file
