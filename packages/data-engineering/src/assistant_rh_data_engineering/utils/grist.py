from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import requests

from .helpers import utc_now_iso

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

# Colonnes écrites par le pipeline (writeback de statut d'ingestion,
# distinctes du suivi manuel statut_ingestion_reelle existant).
WRITEBACK_MANIFEST_COLUMNS: tuple[str, ...] = (
    "statut_ingestion",
    "derniere_ingestion",
    "nb_chunks",
    "hash_contenu",
    "erreur_ingestion",
    "ingere_prod",
    "ingere_staging",
)

# Vocabulaire de statut_ingestion — colonne de statut UNIQUE, partagée entre
# opérateurs et jobs (décision 2026-07-04). Machine à états:
#   (vide)       nouvelle ligne => à ingérer au prochain run
#   ok           écrit par le job: présent et à jour (inchangé compris — la
#                fraîcheur est portée par derniere_ingestion; le détail
#                ingéré/inchangé vit dans rag_ingestion_runs)
#   erreur       écrit par le job: sera retentée au prochain run
#   a_supprimer  posé par un OPÉRATEUR: suppression cascade au prochain run
#   supprime     écrit par le job après la cascade: ligne inactive — la
#                ré-activation se fait en vidant la cellule
# Les dashboards et requêtes de drift filtrent sur ces valeurs exactes.
STATUT_OK = "ok"
STATUT_ERREUR = "erreur"
STATUT_IGNORE = "ignore_inchange"  # détail par document dans les runs, jamais en writeback
STATUT_A_SUPPRIMER = "a_supprimer"
STATUT_SUPPRIME = "supprime"

# Valeurs de statut_ingestion qui rendent la ligne inactive: le pipeline la
# traite comme un document à supprimer/absent, jamais à (ré)ingérer.
STATUTS_INACTIFS: tuple[str, ...] = (STATUT_A_SUPPRIMER, STATUT_SUPPRIME)

MANIFEST_STATUTS: tuple[str, ...] = ("en_vigueur", "abroge")

# Valeurs admises pour la colonne abroge du référentiel.
# (La validation des ajouts unitaires Légifrance/SP arrive en Phase E,
# issue #249, avec le code qui la consomme.)
_ABROGE_VALUES: dict[str, str] = {"": "en_vigueur", "non": "en_vigueur", "oui": "abroge"}


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
    date_publication: str | None = None
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


def _normalize_optional_date(value: Any) -> str | None:
    """Normalise une date Grist sans rendre ce champ optionnel bloquant.

    L'API Grist renvoie les colonnes Date sous forme de timestamp Unix pour
    certaines lignes et de chaîne ISO pour d'autres. PostgreSQL n'accepte pas
    le timestamp entier tel quel dans une colonne ``date``; on convertit donc
    les deux représentations au bord du manifest.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


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

        # Colonne de statut unique: a_supprimer (opérateur) et supprime (job
        # après cascade) rendent la ligne inactive au même titre que le
        # drapeau juridique abroge — sans quoi une ligne déjà supprimée
        # serait ré-ingérée au run suivant.
        statut_ingestion = str(fields.get("statut_ingestion") or "").strip().lower()
        if statut == "en_vigueur" and statut_ingestion in STATUTS_INACTIFS:
            statut = "abroge"

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
                date_publication=_normalize_optional_date(fields.get("date_publication")),
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


# --- Writeback du cycle de vie unifié #289 (réconciliations delta SP / Legi) ---
# Cycle de vie dans `statut` + réalité corpus dans `statut_ingestion_reelle`
# (vocabulaire du matcher de drift, #294).
STATUT_INGERE = "ingere"
STATUT_REEL_INGERE = "ingere"
STATUT_REEL_NON_TROUVE = "non_trouve"

# Réalité corpus PAR ENVIRONNEMENT (colonnes Bool du référentiel). Le doc Grist
# est partagé entre les environnements : seul le run prod écrit le cycle de vie
# canonique (`statut` + métadonnées) ; chaque run coche/décoche son toggle.
INGERE_ENV_COLUMNS: dict[str, str] = {"prod": "ingere_prod", "staging": "ingere_staging"}
CANONICAL_ENV = "prod"


def build_writeback_fields(
    *,
    statut: str,
    statut_reel: str | None = None,
    nb_chunks: int | None = None,
    hash_contenu: str = "",
    erreur: str = "",
    env: str = CANONICAL_ENV,
    corpus_present: bool | None = None,
) -> dict[str, Any]:
    """Champs de writeback d'une ligne du référentiel, selon l'environnement du run.

    Le doc Grist est partagé entre les environnements, donc :
    - ``env == "prod"`` (canonique) : cycle de vie unifié #289 dans ``statut``
      (``ingere``/``erreur``/``supprime``), métadonnées historiques, et
      ``statut_ingestion_reelle`` (réalité corpus vérifiée à ce run, cf. #294) ;
    - tout environnement connu (``INGERE_ENV_COLUMNS``) coche/décoche son toggle
      ``ingere_{env}`` quand la réalité corpus est établie (``corpus_present``) —
      ``None`` = réalité inconnue (ex. échec d'ingestion), toggle non touché.

    Un run staging n'écrit donc JAMAIS le statut canonique : il ne peut pas
    mentir sur l'état prod. Peut retourner un dict vide (rien à écrire).
    """
    fields: dict[str, Any] = {}
    if env == CANONICAL_ENV:
        fields.update(
            {
                "statut": statut,
                "derniere_ingestion": utc_now_iso(),
                "erreur_ingestion": erreur,
            }
        )
        if statut_reel is not None:
            fields["statut_ingestion_reelle"] = statut_reel
        if nb_chunks is not None:
            fields["nb_chunks"] = nb_chunks
        if hash_contenu:
            fields["hash_contenu"] = hash_contenu
    if corpus_present is not None and env in INGERE_ENV_COLUMNS:
        fields[INGERE_ENV_COLUMNS[env]] = corpus_present
    return fields


def build_pdf_writeback_fields(
    *,
    statut: str,
    nb_chunks: int | None = None,
    hash_contenu: str = "",
    erreur: str = "",
    env: str = CANONICAL_ENV,
    corpus_present: bool | None = None,
) -> dict[str, Any]:
    """Writeback PDF séparé par environnement, compatible avec le statut legacy.

    Les pipelines PDF utilisent encore ``statut_ingestion`` comme cycle de vie
    opérateur (``a_supprimer``/``supprime``). La prod reste donc seule à écrire
    ce statut détaillé et ses métadonnées historiques. Chaque environnement
    écrit uniquement son booléen ``ingere_{env}`` quand la présence réelle en
    base est connue.
    """
    fields: dict[str, Any] = {}
    if env == CANONICAL_ENV:
        fields.update(
            {
                "statut_ingestion": statut,
                "derniere_ingestion": utc_now_iso(),
                "erreur_ingestion": erreur,
            }
        )
        if nb_chunks is not None:
            fields["nb_chunks"] = nb_chunks
        if hash_contenu:
            fields["hash_contenu"] = hash_contenu
    if corpus_present is not None and env in INGERE_ENV_COLUMNS:
        fields[INGERE_ENV_COLUMNS[env]] = corpus_present
    return fields


_WRITEBACK_BATCH_SIZE = 100


def writeback_fiches(
    grist: Any,
    updates: "list[tuple[int | None, dict[str, Any]]] | tuple[tuple[int | None, dict[str, Any]], ...]",
    *,
    table_id: str | None = None,
) -> None:
    """Écrit les statuts d'ingestion en Grist par lots (best-effort).

    Un appel API par lot de ``_WRITEBACK_BATCH_SIZE`` records au lieu d'un appel
    par ligne (le cron quotidien parcourt tout le manifest). Les ``record_id``
    ``None`` (document corpus sans ligne Grist) et les fields vides sont
    ignorés. Le writeback ne doit jamais faire échouer l'ingestion.
    """
    records = [{"id": record_id, "fields": dict(fields)} for record_id, fields in updates if record_id is not None and fields]
    for start in range(0, len(records), _WRITEBACK_BATCH_SIZE):
        batch = records[start : start + _WRITEBACK_BATCH_SIZE]
        try:
            grist.update_records(batch, table_id=table_id)
        except Exception as exc:  # noqa: BLE001 — le writeback ne doit pas faire échouer l'ingestion
            ids = ", ".join(str(record["id"]) for record in batch)
            print(f"[warn] writeback Grist échoué pour records {ids}: {exc}")


def writeback_fiche(
    grist: Any,
    record_id: int | None,
    *,
    table_id: str | None = None,
    **fields_kwargs: Any,
) -> None:
    """Writeback d'une seule ligne — cf. ``writeback_fiches``."""
    writeback_fiches(grist, [(record_id, build_writeback_fields(**fields_kwargs))], table_id=table_id)
