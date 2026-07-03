from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

# Contrat de manifest sur le référentiel de sources Grist (La Suite numérique).
# Table unique multi-corpus (décision 2026-07-03, confirmée sur les données
# réelles: source_corpus couvre déjà MI/MASA/MATTE/MSO/RGRH/SP/Légifrance).
# Le pipeline PDF ne lit que les colonnes ci-dessous, filtrées par corpus;
# les colonnes héritées du suivi manuel (cle_matching, statut_cible, ...)
# sont ignorées. Colonne manquante au niveau table => échec franc; ligne
# invalide => rejet tracé, le run continue.
REQUIRED_MANIFEST_COLUMNS: tuple[str, ...] = (
    "source_corpus",  # discriminant corpus (MI, MASA, MATTE, MSO, ...)
    "uid",  # identité stable du document -> short_id en base
    "titre_document",
    "cle_bucket",  # clé du PDF dans la dropzone (colonne ajoutée au référentiel)
    "abroge",  # 'oui' => abrogé; vide/'non' => en vigueur
)

# Colonnes optionnelles: validées si présentes, jamais bloquantes.
OPTIONAL_MANIFEST_COLUMNS: tuple[str, ...] = (
    "date_publication",
    "sous_thematique",
)

# Colonnes écrites par le pipeline (writeback de statut d'ingestion,
# distinctes du suivi manuel statut_ingestion_reelle existant).
WRITEBACK_MANIFEST_COLUMNS: tuple[str, ...] = (
    "statut_ingestion",
    "derniere_ingestion",
    "nb_chunks",
    "hash_contenu",
    "erreur_ingestion",
)

MANIFEST_STATUTS: tuple[str, ...] = ("en_vigueur", "abroge")

# Valeurs admises pour la colonne abroge du référentiel.
_ABROGE_VALUES: dict[str, str] = {"": "en_vigueur", "non": "en_vigueur", "oui": "abroge"}

# Whitelist d'ajouts unitaires Légifrance / Service-Public.
REQUIRED_WHITELIST_COLUMNS: tuple[str, ...] = ("corpus", "id_texte")
WHITELIST_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "legifrance": re.compile(r"^(LEGIARTI|LEGITEXT|JORFTEXT)\d{12}$"),
    "service_public": re.compile(r"^F\d{1,6}$"),
}


class GristError(RuntimeError):
    """Erreur d'accès à l'API Grist."""


class GristContractError(GristError):
    """Le doc Grist ne respecte pas le contrat de colonnes attendu."""


@dataclass
class GristConfig:
    base_url: str
    api_key: str
    doc_id: str
    # Table par défaut (GRIST_TABLE_ID) — surchargée par appel pour les docs
    # multi-tables (manifest + whitelist).
    table_id: str | None = None

    @classmethod
    def from_env(cls) -> "GristConfig":
        base_url = os.getenv("GRIST_API_BASE_URL", "").rstrip("/")
        api_key = os.getenv("GRIST_API_KEY", "")
        doc_id = os.getenv("GRIST_DOC_ID", "")
        table_id = os.getenv("GRIST_TABLE_ID") or None
        missing = [
            name
            for name, value in (
                ("GRIST_API_BASE_URL", base_url),
                ("GRIST_API_KEY", api_key),
                ("GRIST_DOC_ID", doc_id),
            )
            if not value
        ]
        if missing:
            raise GristError(f"Variables d'environnement Grist manquantes: {', '.join(missing)}")
        return cls(base_url=base_url, api_key=api_key, doc_id=doc_id, table_id=table_id)


@dataclass(frozen=True)
class ManifestRow:
    """Ligne de manifest validée, prête pour le pipeline."""

    record_id: int
    corpus: str
    uid: str
    titre: str
    cle_bucket: str
    statut: str
    date_publication: Any = None
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.uid.strip().upper()


@dataclass(frozen=True)
class RejectedRow:
    record_id: int
    uid: str | None
    errors: tuple[str, ...]


@dataclass
class ManifestValidation:
    valid: list[ManifestRow]
    rejected: list[RejectedRow]

    @property
    def ok(self) -> bool:
        return not self.rejected


class GristClient:
    """Client minimal de l'API Grist (records + colonnes + writeback).

    API: https://support.getgrist.com/api/ — GET/PATCH
    /api/docs/{doc_id}/tables/{table_id}/records et .../columns.
    """

    def __init__(self, config: GristConfig | None = None, *, timeout: int = 30):
        self.config = config or GristConfig.from_env()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _resolve_table(self, table_id: str | None) -> str:
        resolved = table_id or self.config.table_id
        if not resolved:
            raise GristError("Aucune table Grist: passer table_id=... ou définir GRIST_TABLE_ID.")
        return resolved

    def _table_url(self, table_id: str, resource: str) -> str:
        return f"{self.config.base_url}/api/docs/{self.config.doc_id}/tables/{table_id}/{resource}"

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise GristError(f"GET {url} -> HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    def list_columns(self, table_id: str | None = None) -> list[str]:
        payload = self._get(self._table_url(self._resolve_table(table_id), "columns"))
        return [str(column.get("id") or "") for column in payload.get("columns", [])]

    def list_records(
        self,
        table_id: str | None = None,
        *,
        filter: dict[str, list[Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Retourne les records bruts: [{"id": int, "fields": {...}}, ...].

        filter suit l'API Grist: {"colonne": [valeurs autorisées]} — match
        exact côté serveur (sensible à la casse), à réserver aux colonnes
        normalisées.
        """
        params = {"filter": json.dumps(filter, ensure_ascii=False)} if filter else None
        payload = self._get(self._table_url(self._resolve_table(table_id), "records"), params=params)
        return list(payload.get("records", []))

    def add_records(self, records: list[dict[str, Any]], table_id: str | None = None) -> list[int]:
        """records: [{"fields": {...}}, ...] — retourne les ids créés."""
        if not records:
            return []
        url = self._table_url(self._resolve_table(table_id), "records")
        response = requests.post(
            url,
            headers=self._headers(),
            json={"records": records},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise GristError(f"POST {url} -> HTTP {response.status_code}: {response.text[:500]}")
        return [int(record.get("id") or 0) for record in response.json().get("records", [])]

    def update_records(self, updates: list[dict[str, Any]], table_id: str | None = None) -> None:
        """updates: [{"id": record_id, "fields": {...}}, ...]."""
        if not updates:
            return
        url = self._table_url(self._resolve_table(table_id), "records")
        response = requests.patch(
            url,
            headers=self._headers(),
            json={"records": updates},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise GristError(f"PATCH {url} -> HTTP {response.status_code}: {response.text[:500]}")

    def writeback_status(self, record_id: int, fields: dict[str, Any], table_id: str | None = None) -> None:
        self.update_records([{"id": record_id, "fields": fields}], table_id=table_id)


def validate_manifest_columns(columns: list[str], required: tuple[str, ...] = REQUIRED_MANIFEST_COLUMNS) -> None:
    """Contrôle doc-niveau: une colonne requise absente => échec franc."""
    missing = [column for column in required if column not in columns]
    if missing:
        raise GristContractError(f"Colonnes manquantes dans la table Grist: {', '.join(missing)} (requises: {', '.join(required)})")


def validate_manifest_records(
    records: list[dict[str, Any]],
    corpus: str,
) -> ManifestValidation:
    """Contrôle ligne-niveau pour un corpus PDF: lignes invalides rejetées,
    le run continue.

    Une ligne est valide si: source_corpus correspond (insensible à la
    casse), uid non vide et unique dans le lot, titre_document et cle_bucket
    non vides, abroge dans {'', 'non', 'oui'}. date_publication est
    optionnelle (référentiel existant sans cette donnée: ne bloque jamais).
    Une ligne sans cle_bucket est rejetée: c'est la liste de travail des
    PDF restant à déposer dans la dropzone.
    """
    expected_corpus = corpus.strip().lower()

    valid: list[ManifestRow] = []
    rejected: list[RejectedRow] = []
    seen_uids: dict[str, int] = {}

    for record in records:
        record_id = int(record.get("id") or 0)
        fields = dict(record.get("fields") or {})

        row_corpus = str(fields.get("source_corpus") or "").strip()
        if row_corpus.lower() != expected_corpus:
            # Table multi-corpus: les lignes des autres corpus (Légifrance,
            # Service-public, autres ministères) ne concernent pas ce run.
            continue

        errors: list[str] = []

        uid = str(fields.get("uid") or "").strip()
        if not uid:
            errors.append("uid vide")
        elif uid.upper() in seen_uids:
            errors.append(f"uid en doublon avec le record {seen_uids[uid.upper()]}")

        titre = str(fields.get("titre_document") or "").strip()
        if not titre:
            errors.append("titre_document vide")

        cle_bucket = str(fields.get("cle_bucket") or "").strip()
        if not cle_bucket:
            errors.append("cle_bucket vide (PDF à déposer dans la dropzone)")

        abroge_raw = str(fields.get("abroge") or "").strip().lower()
        statut = _ABROGE_VALUES.get(abroge_raw)
        if statut is None:
            errors.append(f"abroge invalide: {abroge_raw!r} (attendu: vide, 'non' ou 'oui')")

        if errors:
            rejected.append(
                RejectedRow(
                    record_id=record_id,
                    uid=uid or None,
                    errors=tuple(errors),
                )
            )
            continue

        seen_uids[uid.upper()] = record_id
        valid.append(
            ManifestRow(
                record_id=record_id,
                corpus=row_corpus,
                uid=uid,
                titre=titre,
                cle_bucket=cle_bucket,
                statut=statut,
                date_publication=fields.get("date_publication") or None,
                fields=fields,
            )
        )

    return ManifestValidation(valid=valid, rejected=rejected)


def fetch_validated_manifest(
    client: GristClient,
    corpus: str,
    table_id: str | None = None,
) -> ManifestValidation:
    """Lecture + validation complète du manifest pour un corpus PDF.

    Échec franc si le contrat de colonnes n'est pas respecté; sinon retourne
    lignes valides + lignes rejetées (à écrire en writeback `erreur`).
    Le filtrage par corpus est fait côté client (insensible à la casse).
    """
    validate_manifest_columns(client.list_columns(table_id))
    return validate_manifest_records(client.list_records(table_id), corpus)
