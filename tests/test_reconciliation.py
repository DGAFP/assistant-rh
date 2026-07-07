"""Tests du diff de réconciliation corpus-agnostique (socle #288).

Fonction pure : aucun I/O, on vérifie la classification new/changed/unchanged,
les suppressions typées (abrogated/stale/flagged) et leur confiance.
"""

from __future__ import annotations

from assistant_rh_data_engineering.reconciliation import (
    Confidence,
    CorpusEntry,
    ManifestEntry,
    build_plan,
)


def _manifest(*entries: ManifestEntry) -> dict[str, ManifestEntry]:
    return {entry.uid: entry for entry in entries}


def _corpus(*entries: CorpusEntry) -> dict[str, CorpusEntry]:
    return {entry.uid: entry for entry in entries}


def test_classifies_new_changed_unchanged() -> None:
    manifest = _manifest(
        ManifestEntry("A", content_hash="h-a"),  # absent du corpus -> new
        ManifestEntry("B", content_hash="h-b2"),  # hash différent -> changed
        ManifestEntry("C", content_hash="h-c"),  # hash identique -> unchanged
    )
    corpus = _corpus(
        CorpusEntry("B", content_hash="h-b1", nb_chunks=3),
        CorpusEntry("C", content_hash="h-c", nb_chunks=5),
    )

    plan = build_plan(manifest, corpus)

    assert plan.new == ("A",)
    assert plan.changed == ("B",)
    assert plan.unchanged == ("C",)
    assert plan.to_ingest == ("A", "B")
    assert plan.removals == ()


def test_unknown_hash_forces_reingest() -> None:
    # Hash inconnu d'un côté -> on ne peut pas prouver l'égalité -> changed.
    manifest = _manifest(ManifestEntry("A", content_hash=""))
    corpus = _corpus(CorpusEntry("A", content_hash="h", nb_chunks=2))

    plan = build_plan(manifest, corpus)

    assert plan.changed == ("A",)
    assert plan.unchanged == ()


def test_zero_chunk_retry_reingests_despite_matching_hash() -> None:
    manifest = _manifest(ManifestEntry("A", content_hash="h"))
    corpus = _corpus(CorpusEntry("A", content_hash="h", nb_chunks=0))

    assert build_plan(manifest, corpus).changed == ("A",)
    # corpus tolérant au zéro-chunk (filtre gold légitime) -> unchanged.
    assert build_plan(manifest, corpus, retry_zero_chunk=False).unchanged == ("A",)


def test_stale_document_is_authoritative_removal() -> None:
    # En base, absent du manifest -> stale (Grist a décidé -> autoritaire).
    manifest = _manifest(ManifestEntry("A", content_hash="h"))
    corpus = _corpus(
        CorpusEntry("A", content_hash="h", nb_chunks=2),
        CorpusEntry("Z", content_hash="hz", nb_chunks=1),
    )

    plan = build_plan(manifest, corpus)

    assert [(r.uid, r.reason, r.confidence) for r in plan.removals] == [("Z", "stale", Confidence.AUTHORITATIVE)]
    assert plan.auto_removals == ("Z",)
    assert plan.flagged_removals == ()


def test_abrogated_present_is_authoritative_removal() -> None:
    manifest = _manifest(ManifestEntry("A", content_hash="h", abrogated=True))
    corpus = _corpus(CorpusEntry("A", content_hash="h", nb_chunks=2))

    plan = build_plan(manifest, corpus)

    assert plan.auto_removals == ("A",)
    assert plan.removals[0].reason == "abrogated"
    assert plan.to_ingest == ()  # un abrogé n'est jamais (ré)ingéré


def test_abrogated_absent_is_only_acknowledged() -> None:
    # Marqué abrogé mais déjà absent du corpus : rien à supprimer, juste acquitter.
    manifest = _manifest(ManifestEntry("A", abrogated=True))
    plan = build_plan(manifest, _corpus())

    assert plan.removals == ()
    assert plan.acknowledged == ("A",)


def test_weak_removal_is_flagged_not_auto() -> None:
    # Signal faible (fiche SP absente) -> confiance WEAK, jamais auto-appliqué.
    manifest = _manifest(ManifestEntry("A", content_hash="h", weak_removal=True))
    corpus = _corpus(CorpusEntry("A", content_hash="h", nb_chunks=2))

    plan = build_plan(manifest, corpus)

    assert plan.flagged_removals == ("A",)
    assert plan.auto_removals == ()
    assert plan.removals[0].reason == "flagged"


def test_protected_is_never_removed_or_reingested() -> None:
    # Incident d'acquisition : ni suppression stale, ni suppression abrogée.
    manifest = _manifest(ManifestEntry("A", abrogated=True))
    corpus = _corpus(
        CorpusEntry("A", content_hash="h", nb_chunks=2),  # abrogé mais protégé
        CorpusEntry("Z", content_hash="hz", nb_chunks=1),  # stale mais protégé
    )

    plan = build_plan(manifest, corpus, protected={"A", "Z"})

    assert plan.removals == ()
    assert plan.acknowledged == ()
