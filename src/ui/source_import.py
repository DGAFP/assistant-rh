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


class SourceImportError(ValueError):
    """Entrée invalide dans le formulaire d'import."""


def validate_pdf_bytes(pdf_bytes: bytes) -> None:
    """Contrôle serveur du contenu: le filtre type= de st.file_uploader est
    purement côté client."""
    if not pdf_bytes:
        raise SourceImportError("Fichier vide.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise SourceImportError("Le fichier n'est pas un PDF (signature %PDF- absente).")


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
    """Nom de fichier sûr pour une clé S3: ascii, sans espaces ni chemins."""
    base = os.path.basename(filename or "").strip() or "document.pdf"
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base)
    base = re.sub(r"-+(?=\.)", "", base).strip("-.")
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf" if base else "document.pdf"
    if len(base) > _MAX_FILENAME_LENGTH:
        # Clé S3 bornée: on tronque le nom, pas l'extension.
        base = base[: _MAX_FILENAME_LENGTH - 4].rstrip("-._") + ".pdf"
    return base


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
        if int(record.get("id") or 0) == record_id:
            return record
    return None


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


@dataclass
class DropzoneUploader:
    """Upload d'un PDF vers la dropzone via boto3 (S3-compatible Scaleway).

    Les jobs data-engineering utilisent l'aws CLI (sync massif d'arbres);
    l'UI ne fait qu'un put_object unitaire: boto3 suffit et évite d'embarquer
    la CLI dans l'image Streamlit.
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

    def upload_pdf(self, key: str, pdf_bytes: bytes) -> str:
        try:
            self._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
        except Exception as exc:  # botocore ClientError & co: pas de traceback brut dans l'UI
            raise SourceImportError(f"Échec de l'upload dropzone ({key}): {type(exc).__name__}: {str(exc)[:200]}") from exc
        return f"s3://{self.bucket}/{key}"
