"""Réconciliation delta pour l'ingestion Légifrance (E2.3-b v2, #289).

Branche le socle d'orchestration #288 (``reconciliation.build_plan``) sur
l'ingestion Légifrance. **Modèle « texte suivi » unifié** (décision du
11/07/2026, consolidation sur ``rag_chunks_dgafp``) :

- chaque ligne Grist Légifrance = **un texte suivi en live via PISTE** —
  ``legifrance_code`` (le CGFP : LEGITEXT dans ``id_extraction``, TOC via
  ``legi/tableMatieres``) ou ``legifrance_texte`` (décret/arrêté : JORFTEXT
  dans ``jorftext``/``id_extraction``/``url_legifrance``, TOC via
  ``consult/lawDecree``) ;
- le manifest par article est l'**union des TOCs** des textes suivis ; l'unique
  surface corpus est la table legacy ``rag_chunks_dgafp`` (article-level,
  rattachement par ``cid``) — la table moderne est en cours de décommission ;
- **identité stable = CID chronique** (``article.cid``). L'``id`` de version
  LEGIARTI change à chaque modification : le corpus historique est keyed
  version (bug parseur corrigé dans ``xml_article_parser``), la TOC porte les
  deux → un doc corpus égal à un ``version_id`` d'un texte suivi est un
  **ancien alias d'identité** → ``stale`` autoritaire (migration d'identité,
  remplacé par son cid chronique) ;
- un article du corpus **hors de toutes les TOCs suivies** est inattribuable :
  ``flagged`` (revue opérateur), **jamais** de suppression autoritaire — c'est
  la protection structurelle des articles d'autres textes (les « 244 »).

Les lignes sans ``type_id`` (circulaires, dossiers) restent hors périmètre
(résidu structurel) ; les documents texte-level de la table moderne encore en
base sont protégés et surfacés (``legacy_text_docs``) jusqu'à leur
décommission. Le hash de contenu est le ``sha256`` du markdown silver
(= ``rag_documents.checksum``) — le delta ne recalcule rien.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..reconciliation import Confidence, CorpusEntry, ManifestEntry, ReconciliationPlan, Removal, build_plan
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
_JORFTEXT_RE = re.compile(r"JORFTEXT\d+", re.IGNORECASE)

# Garde anti-suppression-massive : au-delà de ce nombre de `stale` (docs au
# corpus hors manifest), le signal ressemble plus à un état de migration (corpus
# historique keyed version, cf. dry-run staging du 10/07/2026) ou à un manifest
# partiel qu'à une curation volontaire → les stale basculent en `flagged`
# (jamais auto-appliqués), à passer par une revue opérateur. Les abrogations
# (Grist/ETAT), elles, restent autoritaires quel que soit le volume.
DEFAULT_MAX_AUTO_STALE = 50


def _fold(value: Any) -> str:
    """Minuscules + accents retirés (``Légifrance`` → ``legifrance``)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


def is_legifrance(fields: Mapping[str, Any]) -> bool:
    return LEGI_CORPUS_MARKER in _fold(fields.get("source_corpus"))


def is_article_uid(uid: str) -> bool:
    """Un uid d'article — identité du corpus dgafp : cid chronique LEGIARTI,
    ou JORFARTI pour les textes non re-chroniqués côté LEGI (lui aussi stable)."""
    return str(uid or "").upper().startswith(("LEGIARTI", "JORFARTI"))


def extract_jorftext(fields: Mapping[str, Any]) -> str | None:
    """JORFTEXT d'une ligne texte : ``jorftext``, sinon ``id_extraction``,
    sinon extrait de ``url_legifrance`` (…/loda/id/JORFTEXT…)."""
    for source in (fields.get("jorftext"), fields.get("id_extraction"), fields.get("url_legifrance")):
        match = _JORFTEXT_RE.search(str(source or ""))
        if match:
            return match.group(0).upper()
    return None


@dataclass(frozen=True)
class LegifranceManifestRow:
    """Ligne Légifrance du référentiel, résolue pour la réconciliation.

    ``kind`` : ``code`` (uid = LEGITEXT, TOC ``legi/tableMatieres``) ou
    ``texte`` (uid = JORFTEXT, TOC ``consult/lawDecree``). Dans les deux cas la
    ligne représente un **texte suivi** dont les articles sont dérivés de sa
    TOC. Sémantique active/abrogated/juridical/limbo identique à SP.
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
    out_of_scope_uids: tuple[str, ...] = ()  # short_ids déjà mappés, à protéger du stale
    pending_mapping: tuple[int, ...] = ()  # textes actifs sans JORFTEXT résoluble

    @property
    def code_rows(self) -> tuple[LegifranceManifestRow, ...]:
        return tuple(row for row in self.rows if row.kind == "code")

    @property
    def texte_rows(self) -> tuple[LegifranceManifestRow, ...]:
        return tuple(row for row in self.rows if row.kind == "texte")

    @property
    def followed_rows(self) -> tuple[LegifranceManifestRow, ...]:
        """Textes suivis participant au plan (TOC à fetcher si non-limbo)."""
        return self.rows


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
    """Lignes Légifrance du référentiel → textes suivis, dédupliqués par uid.

    - ``type_id=legifrance_code`` : LEGITEXT via ``id_extraction`` (fallback
      colonne ``legitext``). Une ligne code active/abrogée sans LEGITEXT
      résoluble invalide le manifest (plan destructif incalculable).
    - ``type_id=legifrance_texte`` : JORFTEXT via ``jorftext``/``id_extraction``/
      ``url_legifrance``. Une ligne texte active/abrogée sans JORFTEXT est
      surfacée en ``pending_mapping`` : jamais touchée, jamais bloquante.
    - sans ``type_id`` Légifrance : hors périmètre (résidu structurel), compté ;
      son ``document_ids`` éventuel est protégé du stale.
    """
    rows_by_uid: dict[str, LegifranceManifestRow] = {}
    out_of_scope: list[int] = []
    out_of_scope_uids: list[str] = []
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
            jorftext = extract_jorftext(fields)
            if not jorftext:
                if active or abrogated:
                    pending_mapping.append(record_id)
                    print(f"[warn] ligne texte Légifrance Grist {record_id} sans JORFTEXT résoluble: surfacée, jamais touchée.")
                continue
            kind, uid = "texte", jorftext
        else:
            # Résidu structurel (#289) : circulaires/dossiers hors pipeline Legi.
            out_of_scope.append(record_id)
            mapped_uid = str(fields.get("document_ids") or "").strip().upper()
            if mapped_uid:
                out_of_scope_uids.append(mapped_uid)
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
        out_of_scope_uids=tuple(sorted(set(out_of_scope_uids))),
        pending_mapping=tuple(pending_mapping),
    )


# --- Plan ----------------------------------------------------------------------


@dataclass(frozen=True)
class LegifrancePlan:
    """Plan de réconciliation Legi + index de writeback et d'attribution."""

    plan: ReconciliationPlan
    # Writeback AGRÉGÉ par texte suivi : uid de ligne (LEGITEXT/JORFTEXT) -> record_id.
    followed_record_ids: Mapping[str, int]
    # Articles rattachés à chaque texte suivi (cids ∪ version_ids de sa TOC),
    # pour agréger le writeback et attribuer le stale.
    followed_articles: Mapping[str, frozenset[str]]
    protected: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    out_of_scope: tuple[int, ...] = ()
    pending_mapping: tuple[int, ...] = ()
    # Documents texte-level de la table moderne encore en base : protégés
    # jusqu'à la décommission (consolidation dgafp), surfacés au résumé.
    legacy_text_docs: tuple[str, ...] = ()
    # True si le garde anti-suppression-massive a rétrogradé les stale en flagged.
    mass_stale_guard: bool = False


def build_legifrance_plan(
    selection: LegifranceSelection,
    toc_by_text: Mapping[str, Sequence[CodeArticle]],
    silver_checksums: Mapping[str, str],
    corpus: Mapping[str, Mapping[str, Any]],
    *,
    requested: Collection[str] | None = None,
    retry_zero_chunk: bool = True,
    guard_empty_manifest: bool = True,
    max_auto_stale: int | None = DEFAULT_MAX_AUTO_STALE,
    extra_attributions: Mapping[str, str] | None = None,
    extra_chroniques: Mapping[str, str] | None = None,
) -> LegifrancePlan:
    """Adapte référentiel Grist + TOCs PISTE + état corpus au diff ``build_plan``.

    ``extra_attributions`` : ownership VÉRIFIÉE ``uid corpus → uid de texte
    suivi`` pour les articles hors TOC (anciennes versions), établie en amont
    par résolution PISTE ``getArticle``. Un owner inconnu des textes suivis est
    ignoré (fail-closed → flagged).

    ``toc_by_text`` : follow-live PISTE par texte suivi (clé = uid de ligne,
    LEGITEXT ou JORFTEXT ; valeurs = articles avec cid chronique + version_id +
    ETAT). Une TOC absente ou vide pour un texte **actif** invalide le plan.
    Pour un texte **abrogé**, la TOC sert à attribuer ses articles à cascader ;
    absente, ses articles restent inattribuables (flagged, jamais cascadés).

    Attribution du stale : un article du corpus absent du manifest n'est
    ``stale`` autoritaire que s'il est **attribuable** à un texte suivi
    (== un ``cid`` ou un ``version_id`` de sa TOC — ce dernier cas est la
    migration d'identité version→chronique). Sinon → ``flagged``.

    ``requested`` : sous-ensemble ``--uid`` — restreint manifest ET corpus.
    ``max_auto_stale`` : au-delà de ce volume de ``stale``, tous basculent en
    ``flagged`` — ``None`` désactive (migration délibérée).
    """
    requested_set: set[str] | None = None
    if requested is not None:
        requested_set = {str(uid).strip().upper() for uid in requested}
    loaded = {str(uid).strip().upper() for uid in silver_checksums}

    def _wanted(uid: str) -> bool:
        return requested_set is None or uid in requested_set

    corpus_article_uids = {str(uid).strip().upper() for uid in corpus if is_article_uid(str(uid))}
    corpus_text_docs = {str(uid).strip().upper() for uid in corpus if not is_article_uid(str(uid))}

    manifest: dict[str, ManifestEntry] = {}
    followed_record_ids: dict[str, int] = {}
    followed_articles: dict[str, set[str]] = {}
    # attribuable = ∪ (cids ∪ version_ids) des TOCs des textes suivis.
    attributable: set[str] = set()
    # alias (version_id / alias_ids / cid) -> cid chronique de l'article : identité
    # de remplacement d'un ancien alias, pour apparier PRÉCISÉMENT une migration
    # (X est l'ancienne version de C) et non par simple collision de checksum.
    alias_to_chronique: dict[str, str] = {}
    # Les lignes sans type_id sont explicitement hors du pipeline Legi. Quand
    # le matcher a déjà posé leur short_id, elles doivent rester hors du diff
    # stale au lieu d'être supprimées comme des orphelins du manifest.
    protected: set[str] = set(selection.out_of_scope_uids)
    pending: set[str] = set()

    def _add_active(uid: str) -> None:
        if uid in loaded or requested_set is not None:
            manifest[uid] = ManifestEntry(uid, content_hash=silver_checksums.get(uid, ""))
        else:
            # Run complet, artefact hors du lake chargé : protégé + surfacé,
            # jamais un faux échec ni une cascade (même règle que SP).
            pending.add(uid)

    for row in selection.followed_rows:
        followed_record_ids[row.uid] = row.record_id
        arts = followed_articles.setdefault(row.uid, set())
        toc = toc_by_text.get(row.uid)
        if row.active:
            if not toc:
                raise GristContractError(
                    f"TOC PISTE indisponible ou vide pour le texte actif {row.uid}: refus de calculer un plan destructif sur ses articles."
                )
        elif not toc:
            # Texte abrogé/limbo sans TOC : ses articles corpus restent
            # inattribuables → flagged, jamais cascadés à l'aveugle.
            continue
        for article in toc:
            cid = str(article.cid).strip().upper()
            aliases = {str(alias).strip().upper() for alias in (getattr(article, "alias_ids", ()) or ()) if alias}
            aliases.add(cid)
            version_id = str(getattr(article, "version_id", "") or "").strip().upper()
            if version_id:
                aliases.add(version_id)
            arts.update(aliases)
            attributable.update(aliases)
            for alias in aliases:
                alias_to_chronique[alias] = cid
            if not _wanted(cid):
                continue
            if row.limbo:
                # Texte en limbo : jamais ingéré ni supprimé ; ses articles
                # déjà en base sont protégés du stale.
                protected.update(uid for uid in aliases if uid in corpus_article_uids)
            elif row.abrogated or str(article.etat).strip().upper() != VIGUEUR:
                # Retrait opérateur du texte entier OU abrogation calculée à
                # la source (ETAT) : autoritaire.
                manifest[cid] = ManifestEntry(cid, abrogated=True)
            else:
                _add_active(cid)
            # NB : un doc corpus keyed par version_id d'un article suivi n'est
            # PAS ajouté au manifest → stale autoritaire attribuable (migration
            # d'identité version→chronique, remplacé par son cid).

    # Corpus article hors de toutes les TOCs suivies : attribuable via une
    # ownership VÉRIFIÉE en amont (``extra_attributions`` : résolution PISTE
    # ``getArticle`` → texte parent, cf. job) → stale autoritaire scopé
    # (ancienne version d'un texte suivi, ex. L652-1 recodifié). Sinon flagged
    # (weak_removal), jamais de suppression autoritaire — fail-closed,
    # protection structurelle des articles d'autres textes (les « 244 »).
    extras = {str(uid).strip().upper(): str(owner).strip().upper() for uid, owner in (extra_attributions or {}).items()}
    for uid in corpus_article_uids:
        if uid in manifest or uid in attributable or uid in protected or not _wanted(uid):
            continue
        owner = extras.get(uid)
        if owner and owner in followed_articles:
            attributable.add(uid)
            followed_articles[owner].add(uid)
            continue
        manifest[uid] = ManifestEntry(uid, weak_removal=True)

    # Anciennes versions HORS TOC rattachées via getArticle : le job a résolu leur
    # chronique de remplacement (article.cid, validée dans la TOC du texte suivi).
    # On l'enregistre pour que la migration soit reconnue par _is_migration_twin
    # (sinon ces stale autoritaires seraient rétrogradés par le garde et l'INSERT
    # de la chronique re-collisionnerait — P2 bis revue #317). La TOC prime.
    for old_uid, chronique in (extra_chroniques or {}).items():
        alias_to_chronique.setdefault(str(old_uid).strip().upper(), str(chronique).strip().upper())

    # Documents texte-level (table moderne) encore en base : protégés jusqu'à
    # la décommission (consolidation dgafp), jamais gérés par ce delta.
    legacy_text_docs = tuple(sorted(uid for uid in corpus_text_docs if uid not in protected))
    protected.update(corpus_text_docs)

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

    # Jumeaux de MIGRATION D'IDENTITÉ : un stale dont le contenu est repris à
    # l'identique par une chronique à ingérer (même checksum silver) N'EST PAS une
    # purge — le contenu est préservé sous la nouvelle identité. Le garde
    # anti-suppression-massive ne doit donc NI les compter NI les rétrograder :
    # sinon ils quittent auto_removals, l'appairage out-avant-in de ingest_delta
    # ne les cascade plus, et l'INSERT de la chronique re-collisionne sur
    # uq_rag_documents_source_checksum (bug cron : recodification > max_auto_stale).
    silver_by_uid = {str(u).strip().upper(): str(c or "").strip() for u, c in silver_checksums.items()}
    to_ingest = frozenset(plan.new) | frozenset(plan.changed)

    def _is_migration_twin(removal: Removal) -> bool:
        # Jumeau de migration = ANCIENNE VERSION de sa chronique de remplacement
        # (relation TOC alias→cid) ET à contenu identique (même checksum). Le seul
        # match de checksum ne suffit PAS : deux articles NON liés au contenu
        # identique contourneraient sinon le garde (P2 revue #317).
        if removal.reason != "stale" or removal.confidence is not Confidence.AUTHORITATIVE:
            return False
        chronique = alias_to_chronique.get(removal.uid)
        if chronique is None or chronique not in to_ingest:
            return False
        entry = corpus_entries.get(removal.uid)
        stale_checksum = str(entry.content_hash or "").strip() if entry else ""
        return bool(stale_checksum) and stale_checksum == silver_by_uid.get(chronique, "")

    # Garde anti-suppression-massive : un volume anormal de stale (hors migration)
    # trahit un manifest partiel, pas une curation opérateur — on rétrograde ces
    # stale en flagged (WEAK). Les jumeaux de migration restent AUTHORITATIVE.
    mass_stale_guard = False
    stale_auto = [r for r in plan.removals if r.reason == "stale" and r.confidence is Confidence.AUTHORITATIVE and not _is_migration_twin(r)]
    if max_auto_stale is not None and len(stale_auto) > max_auto_stale:
        mass_stale_guard = True
        downgraded = tuple(
            Removal(r.uid, r.reason, Confidence.WEAK)
            if r.reason == "stale" and r.confidence is Confidence.AUTHORITATIVE and not _is_migration_twin(r)
            else r
            for r in plan.removals
        )
        plan = ReconciliationPlan(
            new=plan.new,
            changed=plan.changed,
            unchanged=plan.unchanged,
            removals=downgraded,
            acknowledged=plan.acknowledged,
        )
        print(
            f"[warn] {len(stale_auto)} documents stale (> max_auto_stale={max_auto_stale}): "
            "suppression auto refusée, basculés en flagged (revue opérateur — relancer avec --max-auto-stale ajusté pour une migration délibérée)."
        )

    return LegifrancePlan(
        plan=plan,
        followed_record_ids=dict(followed_record_ids),
        followed_articles={k: frozenset(v) for k, v in followed_articles.items()},
        protected=tuple(sorted(protected)),
        pending=tuple(sorted(pending)),
        out_of_scope=selection.out_of_scope,
        pending_mapping=selection.pending_mapping,
        legacy_text_docs=legacy_text_docs,
        mass_stale_guard=mass_stale_guard,
    )


def plan_summary(lf_plan: LegifrancePlan, *, sample: int = 10) -> dict[str, Any]:
    """Résumé JSON du plan (compteurs + échantillons), même forme que SP."""
    plan = lf_plan.plan
    # Buckets par ce qui sera réellement APPLIQUÉ : `stale`/`abrogated` =
    # suppressions autoritaires (auto), `flagged` = tout signal faible (jamais
    # auto-appliqué), y compris les stale rétrogradés par un garde et les
    # articles inattribuables à un texte suivi.
    abrogated = [r.uid for r in plan.removals if r.reason == "abrogated" and r.confidence is Confidence.AUTHORITATIVE]
    stale = [r.uid for r in plan.removals if r.reason == "stale" and r.confidence is Confidence.AUTHORITATIVE]

    def bucket(uids: Sequence[str]) -> dict[str, Any]:
        ordered = sorted(uids)
        return {"count": len(ordered), "sample": ordered[:sample]}

    return {
        "new": bucket(plan.new),
        "changed": bucket(plan.changed),
        "unchanged": bucket(plan.unchanged),
        "abrogated": bucket(abrogated),
        "stale": bucket(stale),
        "flagged": bucket(plan.flagged_removals),
        "acknowledged": bucket(plan.acknowledged),
        "protected_limbo": bucket(lf_plan.protected),
        "pending_artifact": bucket(lf_plan.pending),
        "legacy_text_docs": bucket(lf_plan.legacy_text_docs),
        "out_of_scope_rows": {"count": len(lf_plan.out_of_scope)},
        "pending_mapping_rows": {"count": len(lf_plan.pending_mapping)},
        "to_ingest": {"count": len(plan.to_ingest)},
        "auto_removals": {"count": len(plan.auto_removals)},
        "mass_stale_guard": lf_plan.mass_stale_guard,
    }


__all__ = [
    "ACTIVE_STATUTS",
    "DEFAULT_MAX_AUTO_STALE",
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
    "extract_jorftext",
    "is_article_uid",
    "is_legifrance",
    "plan_summary",
    "select_legifrance_rows",
    "writeback_fiches",
]
