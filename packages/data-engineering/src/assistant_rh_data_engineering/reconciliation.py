"""Diff de réconciliation corpus-agnostique — socle d'orchestration (#288).

Fonction pure, sans I/O : compare un *manifest* (Grist — ce qui DOIT être dans
le corpus) au *corpus* en base (ce qui y est), et classe chaque ``uid`` en
``new`` / ``changed`` / ``unchanged``, plus les suppressions (``abrogated`` /
``stale`` / ``flagged``) avec un niveau de **confiance** qui pilote le gating de
l'``apply`` : signal autoritaire → suppression auto-applicable ; signal faible →
flag + fenêtre de stabilité (confirmation au run suivant).

Généralise ``pdf_ministry.pipeline.plan_reconciliation`` (qui ne distingue pas
new/changed et ne porte pas la confiance) pour SP + Légifrance + PDF.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum


class Confidence(str, Enum):
    """Confiance d'un signal de suppression.

    ``AUTHORITATIVE`` = déterministe (abrogation ``ETAT``/``end_date``, ligne
    Grist retirée) → suppression auto-applicable. ``WEAK`` = incertain (fiche SP
    absente de la source, successeur hors périmètre) → flag, appliqué seulement
    après confirmation (fenêtre de stabilité).
    """

    AUTHORITATIVE = "authoritative"
    WEAK = "weak"


@dataclass(frozen=True)
class ManifestEntry:
    """Ligne de manifest (Grist) résolue pour un corpus.

    ``uid`` = identité stable (→ ``short_id`` en base). ``content_hash`` vide =
    inconnu (force un re-ingest par prudence). ``abrogated`` = ``abroge=oui`` /
    ``ETAT=ABROGE`` (signal autoritaire). ``weak_removal`` = marqué à retirer sur
    un signal faible (détecteur SP / successeur hors périmètre).
    """

    uid: str
    content_hash: str = ""
    abrogated: bool = False
    weak_removal: bool = False


@dataclass(frozen=True)
class CorpusEntry:
    """Ligne du corpus en base pour un ``uid`` (checksum + nb de chunks)."""

    uid: str
    content_hash: str = ""
    nb_chunks: int = 0


@dataclass(frozen=True)
class Removal:
    """Une suppression planifiée, avec sa raison et sa confiance."""

    uid: str
    reason: str  # "abrogated" | "stale" | "flagged"
    confidence: Confidence


@dataclass(frozen=True)
class ReconciliationPlan:
    """Résultat du diff. Sets disjoints d'``uid`` + suppressions typées."""

    new: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    removals: tuple[Removal, ...] = ()
    # Marqués à supprimer (abrogated/flagged) mais DÉJÀ absents du corpus :
    # rien à supprimer, juste acquitter le statut terminal côté Grist.
    acknowledged: tuple[str, ...] = field(default=())

    @property
    def to_ingest(self) -> tuple[str, ...]:
        """uids à (ré)ingérer = nouveaux + modifiés."""
        return (*self.new, *self.changed)

    def removals_by_confidence(self, confidence: Confidence) -> tuple[str, ...]:
        return tuple(removal.uid for removal in self.removals if removal.confidence is confidence)

    @property
    def auto_removals(self) -> tuple[str, ...]:
        """Suppressions auto-applicables (signal autoritaire)."""
        return self.removals_by_confidence(Confidence.AUTHORITATIVE)

    @property
    def flagged_removals(self) -> tuple[str, ...]:
        """Suppressions à confirmer (signal faible) — jamais auto-appliquées."""
        return self.removals_by_confidence(Confidence.WEAK)


def _is_unchanged(entry: ManifestEntry, state: CorpusEntry, retry_zero_chunk: bool) -> bool:
    # Hash inconnu d'un côté → on ne peut pas prouver l'égalité → re-ingest.
    if not entry.content_hash or not state.content_hash:
        return False
    if entry.content_hash != state.content_hash:
        return False
    # Un doc à zéro chunk malgré un hash égal = ingestion legacy non convergée
    # (leçon audit MATTE) → re-ingest, sauf corpus l'ayant désactivé.
    if retry_zero_chunk and state.nb_chunks <= 0:
        return False
    return True


def build_plan(
    manifest: Mapping[str, ManifestEntry],
    corpus: Mapping[str, CorpusEntry],
    *,
    protected: Collection[str] = (),
    retry_zero_chunk: bool = True,
) -> ReconciliationPlan:
    """Diff pur manifest ↔ corpus.

    - ``new`` : dans le manifest, absent du corpus.
    - ``changed`` : dans les deux, hash différent (ou inconnu).
    - ``unchanged`` : dans les deux, hash identique (et nb_chunks > 0 si
      ``retry_zero_chunk``).
    - ``removals`` : ``abrogated`` (manifest ``abroge``, autoritaire) +
      ``flagged`` (``weak_removal``, faible) + ``stale`` (en base hors manifest,
      autoritaire).
    - ``protected`` : uids dont l'acquisition source a échoué → JAMAIS supprimés
      ni touchés (un incident transitoire ne doit pas purger un doc sain).
    """
    protected_set = {str(uid) for uid in protected}
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    removals: list[Removal] = []
    acknowledged: list[str] = []

    for uid in sorted(manifest):
        entry = manifest[uid]
        if entry.abrogated or entry.weak_removal:
            if uid in protected_set:
                # Ne jamais toucher un doc dont l'acquisition a échoué ce run.
                continue
            if uid not in corpus:
                acknowledged.append(uid)
                continue
            if entry.abrogated:
                removals.append(Removal(uid, "abrogated", Confidence.AUTHORITATIVE))
            else:
                removals.append(Removal(uid, "flagged", Confidence.WEAK))
            continue
        state = corpus.get(uid)
        if state is None:
            new.append(uid)
        elif _is_unchanged(entry, state, retry_zero_chunk):
            unchanged.append(uid)
        else:
            changed.append(uid)

    manifest_keys = set(manifest)
    for uid in sorted(corpus):
        if uid in manifest_keys or uid in protected_set:
            continue
        removals.append(Removal(uid, "stale", Confidence.AUTHORITATIVE))

    return ReconciliationPlan(
        new=tuple(new),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        removals=tuple(removals),
        acknowledged=tuple(acknowledged),
    )
