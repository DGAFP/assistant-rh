"""Réconciliation delta pour l'ingestion Légifrance (E2.3-b, #289).

Branche le socle d'orchestration #288 (``reconciliation.build_plan``) sur
l'ingestion Légifrance, sur le modèle de ``service_public/reconcile.py``
(E2.3-a). Deux familles de lignes dans le référentiel Grist, discriminées par
``type_id`` :

- ``legifrance_code`` — la ligne « code suivi » (CGFP) : ``id_extraction``
  porte le ``LEGITEXT``. Ses articles ne sont PAS listés en Grist : le manifest
  par article est **dérivé du follow-live PISTE** (``tableMatieres`` → CIDs +
  ETAT, cf. E2.2). Un CID ``VIGUEUR`` = attendu au corpus ; un CID non-VIGUEUR
  = abrogation autoritaire ; un article du corpus disparu de la TOC (churn de
  version LEGIARTI, recodification) = ``stale`` autoritaire → cascade.
- ``legifrance_texte`` — un décret/arrêté legacy (JORFTEXT) : 1 ligne = 1
  document, ``document_ids`` porte le ``short_id`` exact en base (posé par le
  matcher #294). Sémantique statut/abroge identique à SP.

Les lignes Légifrance **sans** ``type_id`` (circulaires ingérées via d'autres
pipelines, dossiers…) sont le résidu structurel documenté dans #289 : hors
périmètre Legi, jamais touchées, comptées dans le résumé.

Le hash de contenu est le ``sha256`` du markdown posé en silver dans
``document["checksum"]`` (= ``rag_documents.checksum``) — identique à SP, le
delta ne recalcule rien.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..reconciliation import CorpusEntry, ManifestEntry, ReconciliationPlan, build_plan
from ..utils.grist import (
    STATUT_ERREUR,
    STATUT_INGERE,
    STATUT_REEL_INGERE,
    STATUT_REEL_NON_TROUVE,
    STATUT_SUPPRIME,
    GristContractError,
    build_writeback_fields,
    writeback_fiches,
)
from .piste import CodeArticle

# --- Sélection Légifrance dans le référentiel Grist ----------------------------

LEGI_CORPUS_MARKER = "legifrance"
TYPE_CODE = "legifrance_code"
TYPE_TEXTE = "legifrance_texte"
# Statuts exprimant l'intention d'avoir le document au corpus / de le retirer —
# mêmes valeurs que SP (un test anti-drift croise les deux).
ACTIVE_STATUTS: frozenset[str] = frozenset({"a_ingerer", "ingere", "erreur"})
REMOVAL_STATUTS: frozenset[str] = frozenset({"a_supprimer", "supprime"})

VIGUEUR = "VIGUEUR"


def _fold(value: Any) -> str:
    """Minuscules + accents retirés (``Légifrance`` → ``legifrance``)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


def is_legifrance(fields: Mapping[str, Any]) -> bool:
    return LEGI_CORPUS_MARKER in _fold(fields.get("source_corpus"))


def is_article_uid(uid: str) -> bool:
    """Routage table : les articles du code vivent dans la table legacy
    (``rag_chunks_dgafp``, rattachement par ``cid``), le reste dans la table
    moderne (``rag_chunks_legifrance``)."""
    return str(uid or "").upper().startswith("LEGIARTI")


@dataclass(frozen=True)
class LegifranceManifestRow:
    """Ligne Légifrance du référentiel, résolue pour la réconciliation.

    ``kind`` : ``code`` (uid = LEGITEXT, articles dérivés de la TOC PISTE) ou
    ``texte`` (uid = ``document_ids`` = short_id en base). Sémantique
    active/abrogated/juridical/limbo identique à SP.
    """

    record_id: int
    kind: str  # "code" | "texte"
    uid: str
    active: bool
    abrogated: bool
    juridical: bool = False
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def limbo(self) -> bool:
        return not self.active and not self.abrogated


@dataclass(frozen=True)
class LegifranceSelection:
    """Lignes retenues + comptage du hors-périmètre (résidu structurel #289)."""

    rows: tuple[LegifranceManifestRow, ...]
    out_of_scope: tuple[int, ...] = ()  # record_ids sans type_id Légifrance
    pending_mapping: tuple[int, ...] = ()  # textes actifs sans document_ids (matcher pas passé)

    @property
    def code_rows(self) -> tuple[LegifranceManifestRow, ...]:
        return tuple(row for row in self.rows if row.kind == "code")

    @property
    def texte_rows(self) -> tuple[LegifranceManifestRow, ...]:
        return tuple(row for row in self.rows if row.kind == "texte")


def _precedence(row: LegifranceManifestRow) -> int:
    # Doublon d'uid : abrogation juridique > intention active > suppression
    # opérateur > limbo (même règle que SP).
    if row.juridical:
        return 3
    if row.active:
        return 2
    if row.abrogated:
        return 1
    return 0


def select_legifrance_rows(records: Iterable[Mapping[str, Any]]) -> LegifranceSelection:
    """Lignes Légifrance du référentiel → manifest rows, dédupliquées par uid.

    - ``type_id=legifrance_code`` : le LEGITEXT vient de ``id_extraction``
      (fallback colonne ``legitext``). Une ligne code active/abrogée sans
      LEGITEXT résoluble invalide le manifest (plan destructif incalculable).
    - ``type_id=legifrance_texte`` : l'uid vient de ``document_ids`` (short_id
      en base). Une ligne texte active/abrogée sans mapping est surfacée en
      ``pending_mapping`` (le matcher #294 n'est pas passé) : jamais touchée,
      jamais bloquante.
    - sans ``type_id`` Légifrance : hors périmètre (résidu structurel), compté.
    """
    rows_by_uid: dict[str, LegifranceManifestRow] = {}
    out_of_scope: list[int] = []
    pending_mapping: list[int] = []
    for record in records:
        fields = record.get("fields") or {}
        if not is_legifrance(fields):
            continue
        record_id = int(record.get("id") or 0)
        type_id = _fold(fields.get("type_id"))
        statut = _fold(fields.get("statut"))
        abroge = _fold(fields.get("abroge")) == "oui"
        abrogated = abroge or statut in REMOVAL_STATUTS
        active = (statut in ACTIVE_STATUTS) and not abrogated

        if type_id == TYPE_CODE:
            raw = str(fields.get("id_extraction") or fields.get("legitext") or "").strip().upper()
            if not raw.startswith("LEGITEXT"):
                if active or abrogated:
                    raise GristContractError(f"Ligne code Légifrance Grist {record_id} sans LEGITEXT: refus de calculer un plan destructif.")
                print(f"[warn] ligne code Légifrance Grist {record_id} sans LEGITEXT ignorée (limbo).")
                continue
            kind, uid = "code", raw
        elif type_id == TYPE_TEXTE:
            uid = str(fields.get("document_ids") or "").strip().upper()
            if not uid:
                if active or abrogated:
                    pending_mapping.append(record_id)
                    print(f"[warn] ligne texte Légifrance Grist {record_id} sans document_ids (matcher pas passé): surfacée, jamais touchée.")
                continue
            kind = "texte"
        else:
            # Résidu structurel (#289) : circulaires/dossiers hors pipeline Legi.
            out_of_scope.append(record_id)
            continue

        row = LegifranceManifestRow(
            record_id=record_id,
            kind=kind,
            uid=uid,
            active=active,
            abrogated=abrogated,
            juridical=abroge,
            fields=dict(fields),
        )
        existing = rows_by_uid.get(uid)
        if existing is None or _precedence(row) > _precedence(existing):
            rows_by_uid[uid] = row
    return LegifranceSelection(
        rows=tuple(sorted(rows_by_uid.values(), key=lambda r: r.uid)),
        out_of_scope=tuple(out_of_scope),
        pending_mapping=tuple(pending_mapping),
    )


# --- Plan ----------------------------------------------------------------------


@dataclass(frozen=True)
class LegifrancePlan:
    """Plan de réconciliation Legi + index de writeback et de routage."""

    plan: ReconciliationPlan
    # Writeback per-ligne (textes) : uid -> record_id.
    texte_record_ids: Mapping[str, int]
    # Writeback agrégé (codes suivis) : LEGITEXT -> record_id.
    code_record_ids: Mapping[str, int]
    # Articles rattachés à chaque code suivi (uids TOC ∪ corpus), pour agréger.
    code_articles: Mapping[str, frozenset[str]]
    protected: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    out_of_scope: tuple[int, ...] = ()
    pending_mapping: tuple[int, ...] = ()


def build_legifrance_plan(
    selection: LegifranceSelection,
    toc_by_legitext: Mapping[str, Sequence[CodeArticle]],
    silver_checksums: Mapping[str, str],
    corpus: Mapping[str, Mapping[str, Any]],
    *,
    requested: Collection[str] | None = None,
    retry_zero_chunk: bool = True,
    guard_empty_manifest: bool = True,
) -> LegifrancePlan:
    """Adapte référentiel Grist + TOC PISTE + état corpus au diff ``build_plan``.

    ``toc_by_legitext`` : follow-live PISTE par code actif (CIDs + ETAT). Une
    TOC absente ou vide pour un code **actif** invalide le plan : sans elle,
    tous ses articles du corpus seraient classés ``stale`` → purge (~2500 docs)
    sur un simple incident API. ``requested`` : sous-ensemble ``--uid`` (CID ou
    short_id texte) — restreint manifest ET corpus comme en SP.
    """
    requested_set: set[str] | None = None
    if requested is not None:
        requested_set = {str(uid).strip().upper() for uid in requested}
    loaded = {str(uid).strip().upper() for uid in silver_checksums}

    def _wanted(uid: str) -> bool:
        return requested_set is None or uid in requested_set

    corpus_article_uids = {str(uid).strip().upper() for uid in corpus if is_article_uid(str(uid))}

    manifest: dict[str, ManifestEntry] = {}
    texte_record_ids: dict[str, int] = {}
    code_record_ids: dict[str, int] = {}
    code_articles: dict[str, set[str]] = {}
    protected: set[str] = set()
    pending: set[str] = set()

    def _add_active(uid: str) -> None:
        if uid in loaded or requested_set is not None:
            manifest[uid] = ManifestEntry(uid, content_hash=silver_checksums.get(uid, ""))
        else:
            # Run complet, artefact hors du lake chargé : protégé + surfacé,
            # jamais un faux échec ni une cascade (même règle que SP).
            pending.add(uid)

    for row in selection.texte_rows:
        if not _wanted(row.uid):
            continue
        texte_record_ids[row.uid] = row.record_id
        if row.abrogated:
            manifest[row.uid] = ManifestEntry(row.uid, abrogated=True)
        elif row.active:
            _add_active(row.uid)
        else:
            protected.add(row.uid)

    for row in selection.code_rows:
        code_record_ids[row.uid] = row.record_id
        arts = code_articles.setdefault(row.uid, set())
        if row.active:
            toc = toc_by_legitext.get(row.uid)
            if not toc:
                raise GristContractError(
                    f"TOC PISTE indisponible ou vide pour le code actif {row.uid}: refus de calculer un plan destructif sur ses articles."
                )
            for article in toc:
                uid = str(article.cid).strip().upper()
                arts.add(uid)
                if not _wanted(uid):
                    continue
                if str(article.etat).strip().upper() == VIGUEUR:
                    _add_active(uid)
                else:
                    # Abrogation calculée à la source (ETAT) : autoritaire.
                    manifest[uid] = ManifestEntry(uid, abrogated=True)
            # Un article du corpus absent de la TOC (recodification, churn de
            # CID) n'est PAS ajouté au manifest → classé stale par build_plan.
            arts.update(corpus_article_uids)
        elif row.abrogated:
            # Code entier retiré (opérateur) : cascade de tous ses articles
            # présents au corpus.
            for uid in corpus_article_uids:
                arts.add(uid)
                if _wanted(uid):
                    manifest[uid] = ManifestEntry(uid, abrogated=True)
        else:
            # Code en limbo : ses articles ne sont jamais touchés.
            arts.update(corpus_article_uids)
            protected.update(uid for uid in corpus_article_uids if _wanted(uid))

    if not selection.code_rows and corpus_article_uids:
        # Aucun code suivi en Grist mais des articles au corpus : sans ligne
        # code on ne peut pas distinguer « retrait volontaire » d'un fetch
        # partiel → protégés (jamais de purge silencieuse de ~2500 articles).
        protected.update(uid for uid in corpus_article_uids if _wanted(uid))

    corpus_entries: dict[str, CorpusEntry] = {}
    for raw_uid, state in corpus.items():
        uid = str(raw_uid).strip().upper()
        if not _wanted(uid):
            continue
        corpus_entries[uid] = CorpusEntry(
            uid,
            content_hash=str(state.get("checksum") or ""),
            nb_chunks=int(state.get("nb_chunks") or 0),
        )

    # Garde anti-purge : ne s'arme que sur un fetch Grist vide (aucune ligne
    # Legi), pas sur un manifest vidé par --uid (cf. E2.3-a).
    effective_guard = guard_empty_manifest and not selection.rows
    plan = build_plan(
        manifest,
        corpus_entries,
        protected=protected | pending,
        retry_zero_chunk=retry_zero_chunk,
        guard_empty_manifest=effective_guard,
    )
    return LegifrancePlan(
        plan=plan,
        texte_record_ids=dict(texte_record_ids),
        code_record_ids=dict(code_record_ids),
        code_articles={k: frozenset(v) for k, v in code_articles.items()},
        protected=tuple(sorted(protected)),
        pending=tuple(sorted(pending)),
        out_of_scope=selection.out_of_scope,
        pending_mapping=selection.pending_mapping,
    )


def plan_summary(lf_plan: LegifrancePlan, *, sample: int = 10) -> dict[str, Any]:
    """Résumé JSON du plan (compteurs + échantillons), même forme que SP."""
    plan = lf_plan.plan
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
        "protected_limbo": bucket(lf_plan.protected),
        "pending_artifact": bucket(lf_plan.pending),
        "out_of_scope_rows": {"count": len(lf_plan.out_of_scope)},
        "pending_mapping_rows": {"count": len(lf_plan.pending_mapping)},
        "to_ingest": {"count": len(plan.to_ingest)},
        "auto_removals": {"count": len(plan.auto_removals)},
    }


__all__ = [
    "ACTIVE_STATUTS",
    "REMOVAL_STATUTS",
    "STATUT_ERREUR",
    "STATUT_INGERE",
    "STATUT_REEL_INGERE",
    "STATUT_REEL_NON_TROUVE",
    "STATUT_SUPPRIME",
    "GristContractError",
    "LegifranceManifestRow",
    "LegifrancePlan",
    "LegifranceSelection",
    "build_legifrance_plan",
    "build_writeback_fields",
    "is_article_uid",
    "is_legifrance",
    "plan_summary",
    "select_legifrance_rows",
    "writeback_fiches",
]
