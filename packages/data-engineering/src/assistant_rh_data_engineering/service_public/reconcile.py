"""Réconciliation delta pour l'ingestion Service-Public (E2.3-a, #289).

Branche le socle d'orchestration #288 (``reconciliation.build_plan``) sur
l'ingestion Service-Public. Au lieu de l'``upsert-all`` historique, un run delta :

- lit le **référentiel Grist** (source de vérité de la sélection : quelles fiches
  doivent être au corpus, lesquelles sont abrogées / à supprimer) ;
- lit l'**état corpus** en base (``list_short_ids_with_checksum``) ;
- classe chaque fiche via ``build_plan`` en ``new`` / ``changed`` / ``unchanged``
  + suppressions typées (abrogée/retirée → cascade autoritaire) ;
- ne (ré)ingère que ``new`` + ``changed`` (les inchangées sont sautées), cascade
  les suppressions, et écrit le **statut** en retour dans Grist.

Le hash de contenu par fiche est le ``doc_text_hash`` calculé en silver et stocké
dans ``rag_documents.checksum`` (cf. ``service_public/silver.py``). Le delta ne
recalcule donc rien : il compare le ``checksum`` de l'artefact silver au
``checksum`` déjà en base.

La sélection SP dans le référentiel multi-corpus DOIT rester cohérente avec
``scripts/generate_service_public_config.py`` (qui génère la config consommée par
le job) : même marqueur de corpus, même extraction de F-code, mêmes statuts
actifs. Un test de non-régression croise les deux (``ACTIVE_STATUTS`` ↔
``WANT_STATUTS``).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..reconciliation import CorpusEntry, ManifestEntry, ReconciliationPlan, build_plan
from ..utils.grist import (
    CANONICAL_ENV,
    INGERE_ENV_COLUMNS,
    STATUT_ERREUR,
    STATUT_INGERE,
    STATUT_REEL_INGERE,
    STATUT_REEL_NON_TROUVE,
    STATUT_SUPPRIME,
    GristContractError,
    build_writeback_fields,
    writeback_fiche,
    writeback_fiches,
)

# --- Sélection SP dans le référentiel Grist (miroir du générateur E2.1) --------

SP_CORPUS_MARKER = "service-public"
# Statuts (colonne `statut`, ex-`statut_cible`) exprimant l'intention d'avoir la
# fiche au corpus. Identique à WANT_STATUTS du générateur E2.1.
ACTIVE_STATUTS: frozenset[str] = frozenset({"a_ingerer", "ingere", "erreur"})
# Intentions de suppression opérateur / état terminal. abroge=oui (juridique) est
# traité en plus, quel que soit le statut.
REMOVAL_STATUTS: frozenset[str] = frozenset({"a_supprimer", "supprime"})
_F_CODE_RE = re.compile(r"F\d+", re.IGNORECASE)

# Cycle de vie unifié dans `statut` (#289), réalité `statut_ingestion_reelle`
# et toggles par environnement : constantes et writeback partagés SP/Legi dans
# ``utils.grist`` (ré-exportés ici pour compat).


def is_service_public(fields: Mapping[str, Any]) -> bool:
    return SP_CORPUS_MARKER in str(fields.get("source_corpus") or "").strip().lower()


def extract_fiche_id(fields: Mapping[str, Any]) -> str | None:
    """F-code de la ligne : ``id_extraction``, sinon le titre, sinon l'``uid``."""
    for source in (fields.get("id_extraction"), fields.get("titre_document"), fields.get("uid")):
        match = _F_CODE_RE.search(str(source or ""))
        if match:
            return match.group(0).upper()
    return None


@dataclass(frozen=True)
class ServicePublicManifestRow:
    """Ligne Service-Public du référentiel Grist, résolue pour la réconciliation.

    ``active`` : à avoir au corpus (statut ``a_ingerer``/``ingere``, non abrogée).
    ``abrogated`` : à retirer (``abroge=oui`` ou statut ``a_supprimer``/``supprime``)
    → suppression autoritaire. ``juridical`` : abrogation juridique (``abroge=oui``),
    signal le plus fort — ne peut jamais être écrasé par une ligne active en cas de
    F-code dupliqué. Une ligne ni active ni abrogée est « en limbo » (ex.
    ``en_attente``, ``a_extraire``) : le run ne la touche pas (protégée de la cascade
    stale). ``record_id`` sert au writeback (clé de ligne Grist).
    """

    record_id: int
    uid: str
    active: bool
    abrogated: bool
    juridical: bool = False
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def limbo(self) -> bool:
        return not self.active and not self.abrogated


def _precedence(row: ServicePublicManifestRow) -> int:
    # F-code dupliqué sur plusieurs lignes. L'abrogation juridique (abroge=oui) est
    # le signal le plus fort : une ligne active dupliquée ne doit JAMAIS ré-autoriser
    # une fiche juridiquement abrogée. Ensuite l'intention active prime sur une
    # suppression opérateur (un opérateur qui ré-ingère l'emporte), puis le limbo.
    if row.juridical:
        return 3
    if row.active:
        return 2
    if row.abrogated:
        return 1
    return 0


def select_manifest_rows(records: Iterable[Mapping[str, Any]]) -> list[ServicePublicManifestRow]:
    """Lignes SP du référentiel → manifest rows, dédupliquées par F-code.

    Ignore les lignes d'un autre corpus. Une ligne Service-Public active ou
    abrogée sans F-code résoluble invalide le manifest : continuer ferait
    disparaître cette ligne du diff et pourrait classer sa fiche existante comme
    ``stale`` autoritaire. Une ligne **limbo** sans F-code (brouillon opérateur
    en cours de saisie) est en revanche ignorée avec un warning : elle ne
    participe ni à l'ingestion ni à la cascade, la skipper ne rend aucun plan
    destructif possible — un brouillon ne doit pas bloquer le cron quotidien.
    """
    rows_by_uid: dict[str, ServicePublicManifestRow] = {}
    for record in records:
        fields = record.get("fields") or {}
        if not is_service_public(fields):
            continue
        statut = str(fields.get("statut") or "").strip().lower()
        abroge = str(fields.get("abroge") or "").strip().lower() == "oui"
        abrogated = abroge or statut in REMOVAL_STATUTS
        active = (statut in ACTIVE_STATUTS) and not abrogated
        uid = extract_fiche_id(fields)
        if not uid:
            record_id = int(record.get("id") or 0)
            if active or abrogated:
                raise GristContractError(f"Ligne Service-Public Grist {record_id} sans F-code résoluble: refus de calculer un plan destructif.")
            print(f"[warn] ligne Service-Public Grist {record_id} sans F-code résoluble ignorée (limbo, brouillon en cours de saisie).")
            continue
        row = ServicePublicManifestRow(
            record_id=int(record.get("id") or 0),
            uid=uid,
            active=active,
            abrogated=abrogated,
            juridical=abroge,
            fields=dict(fields),
        )
        existing = rows_by_uid.get(uid)
        if existing is None or _precedence(row) > _precedence(existing):
            rows_by_uid[uid] = row
    return sorted(rows_by_uid.values(), key=lambda r: r.uid)


@dataclass(frozen=True)
class ServicePublicPlan:
    """Plan de réconciliation SP + index de writeback."""

    plan: ReconciliationPlan
    record_ids: Mapping[str, int]
    protected: tuple[str, ...] = ()
    # Fiches actives en Grist mais absentes du lake chargé sur un run complet
    # (config en retard sur Grist) : ni ingérées ni supprimées, à construire
    # quand la config est régénérée. Surfacées séparément du limbo.
    pending: tuple[str, ...] = ()


def build_service_public_plan(
    manifest_rows: Sequence[ServicePublicManifestRow],
    silver_checksums: Mapping[str, str],
    corpus: Mapping[str, Mapping[str, Any]],
    *,
    requested: Collection[str] | None = None,
    retry_zero_chunk: bool = True,
    guard_empty_manifest: bool = True,
) -> ServicePublicPlan:
    """Adapte le référentiel SP + l'état corpus au diff pur ``build_plan``.

    ``silver_checksums`` : ``uid (upper) -> checksum`` des artefacts silver
    disponibles (côté manifest). Une fiche active sans checksum silver a un
    ``content_hash`` vide → forcée en (re)ingest par ``build_plan``.
    ``corpus`` : sortie de ``list_short_ids_with_checksum`` (clés déjà en upper).
    ``requested`` : sous-ensemble ``--fiche-id``. Restreint le manifest ET le
    corpus vus par le diff, pour qu'un run ciblé ne cascade jamais le reste du
    corpus (les fiches hors sous-ensemble ne sont ni ingérées ni supprimées).
    """
    requested_set: set[str] | None = None
    if requested is not None:
        requested_set = {str(uid).strip().upper() for uid in requested}
    # Fiches dont l'artefact silver a été chargé ce run (donc ingérables).
    loaded = {str(uid).strip().upper() for uid in silver_checksums}

    manifest: dict[str, ManifestEntry] = {}
    record_ids: dict[str, int] = {}
    protected: set[str] = set()
    pending: set[str] = set()
    for row in manifest_rows:
        if requested_set is not None and row.uid not in requested_set:
            continue
        record_ids[row.uid] = row.record_id
        if row.abrogated:
            # La suppression ne dépend que du manifest + du corpus, jamais d'un
            # artefact du lake.
            manifest[row.uid] = ManifestEntry(row.uid, abrogated=True)
        elif row.active:
            if row.uid in loaded or requested_set is not None:
                # Ingérable (artefact chargé) OU explicitement demandé via
                # --fiche-id : dans ce dernier cas l'échec « artefact absent »
                # est le bon signal opérateur.
                manifest[row.uid] = ManifestEntry(row.uid, content_hash=silver_checksums.get(row.uid, ""))
            else:
                # Run complet, fiche active hors du lake chargé (config en retard
                # sur Grist) : jamais un faux-échec sur une fiche saine — protégée
                # (ni ingérée ni supprimée), et surfacée dans le résumé.
                pending.add(row.uid)
        else:
            # En limbo : jamais ingérée ni supprimée. `protected` empêche le
            # classement `stale` si la fiche est déjà en base.
            protected.add(row.uid)

    corpus_entries: dict[str, CorpusEntry] = {}
    for raw_uid, state in corpus.items():
        uid = str(raw_uid).strip().upper()
        if requested_set is not None and uid not in requested_set:
            continue
        corpus_entries[uid] = CorpusEntry(
            uid,
            content_hash=str(state.get("checksum") or ""),
            nb_chunks=int(state.get("nb_chunks") or 0),
        )

    # Garde-fou anti-purge : ne s'arme QUE sur un fetch Grist vide (aucune ligne SP
    # du tout), pas sur un manifest rendu vide par le filtre --fiche-id — sinon un
    # run ciblé de suppression n'auto-supprimerait jamais (divergence vs run complet).
    effective_guard = guard_empty_manifest and not manifest_rows
    plan = build_plan(
        manifest,
        corpus_entries,
        protected=protected | pending,
        retry_zero_chunk=retry_zero_chunk,
        guard_empty_manifest=effective_guard,
    )
    return ServicePublicPlan(
        plan=plan,
        record_ids=dict(record_ids),
        protected=tuple(sorted(protected)),
        pending=tuple(sorted(pending)),
    )


def plan_summary(sp_plan: ServicePublicPlan, *, sample: int = 10) -> dict[str, Any]:
    """Résumé JSON du plan (compteurs + échantillons) — AC de #176."""
    plan = sp_plan.plan
    removals_by_reason: dict[str, list[str]] = {}
    for removal in plan.removals:
        removals_by_reason.setdefault(removal.reason, []).append(removal.uid)

    def bucket(uids: Sequence[str]) -> dict[str, Any]:
        ordered = sorted(uids)
        return {"count": len(ordered), "sample": ordered[:sample]}

    return {
        "new": bucket(plan.new),
        "changed": bucket(plan.changed),
        "unchanged": bucket(plan.unchanged),
        "abrogated": bucket(removals_by_reason.get("abrogated", [])),
        "stale": bucket(removals_by_reason.get("stale", [])),
        "flagged": bucket(removals_by_reason.get("flagged", [])),
        "acknowledged": bucket(plan.acknowledged),
        "protected_limbo": bucket(sp_plan.protected),
        "pending_artifact": bucket(sp_plan.pending),
        "to_ingest": {"count": len(plan.to_ingest)},
        "auto_removals": {"count": len(plan.auto_removals)},
    }


# Ré-export des constantes de statut et du writeback pour l'appelant.
__all__ = [
    "ACTIVE_STATUTS",
    "CANONICAL_ENV",
    "GristContractError",
    "INGERE_ENV_COLUMNS",
    "REMOVAL_STATUTS",
    "STATUT_ERREUR",
    "STATUT_INGERE",
    "STATUT_REEL_INGERE",
    "STATUT_REEL_NON_TROUVE",
    "STATUT_SUPPRIME",
    "ServicePublicManifestRow",
    "ServicePublicPlan",
    "build_service_public_plan",
    "build_writeback_fields",
    "extract_fiche_id",
    "is_service_public",
    "plan_summary",
    "select_manifest_rows",
    "writeback_fiche",
    "writeback_fiches",
]
