"""Tests des lignes d'index additives R2 (legifrance/summary_rows.py).

Invariants du design (cf. r2_design, revue stratégies qualité RAG §2.3) :
- l'EMBEDDING encode le résumé, chunk_text reste le texte AUTHENTIQUE servi ;
- chunk_id stable ``{cid}_r2s`` (upsert idempotent, purge par cid du delta) ;
- ``index_variant`` = marqueur + clé de fraîcheur version+checksum ;
- la projection legacy (LegifranceDbWriter) transporte la ligne telle quelle.
"""

from __future__ import annotations

import pytest
from assistant_rh_data_engineering.legifrance import summary_rows as sr
from assistant_rh_data_engineering.legifrance.db import LEGACY_TARGET_COLUMNS, LegifranceDbWriter

ARTICLE_ROW = {
    "cid": "LEGIARTI000044420769",
    "chunk_id": "LEGIARTI000044420769_0",
    "chunk_text": (
        "Code général de la fonction publique\nArticle L123-2\n\nL'agent contractuel ne peut occuper un autre emploi permanent à temps complet."
    ),
    "number": "L123-2",
    "title": "Code général de la fonction publique",
    "full_title": "Code général de la fonction publique",
    "category": "CODE",
    "url": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000044420769",
    "subtitles": "PARTIE LÉGISLATIVE > Livre Ier > Titre II > Chapitre III : Règles de cumul",
    "section_parent_cid": "LEGISCTA000044427821",
    "section_parent_titre": "Section 1 : Dispositions communes",
    "status": "VIGUEUR",
}

SUMMARY = "Concerne les agents contractuels et le cumul de plusieurs emplois publics : interdiction de principe, dérogations possibles."
EMBEDDING = [0.1] * 8


def test_summary_chunk_id_fits_legacy_column() -> None:
    chunk_id = sr.summary_chunk_id(ARTICLE_ROW["cid"])
    assert chunk_id == "LEGIARTI000044420769_r2s"
    assert len(chunk_id) <= 64  # VARCHAR(64) de rag_chunks_dgafp


def test_build_index_variant_tracks_version_and_checksum() -> None:
    v1 = sr.build_index_variant("r2s1-model-p1", "texte A", embed_model="emb-m3")
    assert v1.startswith("r2_summary/r2s1-model-p1+embed-emb-m3/")
    assert sr.is_summary_variant(v1)
    assert not sr.is_summary_variant(None)
    assert not sr.is_summary_variant("")
    # Changement de logique/modèle OU de texte source -> variant différent.
    assert sr.build_index_variant("r2s2-model-p1", "texte A", embed_model="emb-m3") != v1
    assert sr.build_index_variant("r2s1-model-p1", "texte B", embed_model="emb-m3") != v1


def test_build_summary_chunk_row_embeds_summary_serves_authentic_text() -> None:
    row = sr.build_summary_chunk_row(ARTICLE_ROW, SUMMARY, EMBEDDING, summarizer_version="r2s1-model-p1", embed_model="emb-m3")

    # Le résumé TROUVE (embedding + text), il ne DIT jamais (chunk_text servi).
    assert row["chunk_text"] == ARTICLE_ROW["chunk_text"]
    assert row["text"] == SUMMARY
    assert row["embedding_m3"] == EMBEDDING

    assert row["chunk_id"] == "LEGIARTI000044420769_r2s"
    assert row["index_variant"] == sr.build_index_variant("r2s1-model-p1", ARTICLE_ROW["chunk_text"], embed_model="emb-m3")
    assert row["_targets"] == ["legacy"]
    assert row["chunk_number"] is None
    # Méta copiées: la ligne est indiscernable au serving (pills, lookups).
    for column in ("number", "title", "full_title", "category", "url", "section_parent_cid", "section_parent_titre", "status"):
        assert row[column] == ARTICLE_ROW[column]


def test_build_summary_chunk_row_requires_embedding_and_summary() -> None:
    # Piège backfill: une ligne R2 sans vecteur serait ré-embeddée depuis le
    # texte authentique par jobs/embeddings_backfill.py -> refus franc.
    with pytest.raises(ValueError, match="embedding"):
        sr.build_summary_chunk_row(ARTICLE_ROW, SUMMARY, [], summarizer_version="v", embed_model="emb-m3")
    with pytest.raises(ValueError, match="résumé vide"):
        sr.build_summary_chunk_row(ARTICLE_ROW, "  ", EMBEDDING, summarizer_version="v", embed_model="emb-m3")
    with pytest.raises(ValueError, match="cid"):
        sr.build_summary_chunk_row({**ARTICLE_ROW, "cid": ""}, SUMMARY, EMBEDDING, summarizer_version="v", embed_model="emb-m3")


def test_index_variant_column_is_a_legacy_target_column() -> None:
    # La colonne marqueur fait partie du schéma géré par _ensure_table
    # (mécanisme de migration natif de rag_chunks_dgafp).
    assert LEGACY_TARGET_COLUMNS.get("index_variant") == "TEXT"


def test_projection_legacy_preserves_summary_row() -> None:
    row = sr.build_summary_chunk_row(ARTICLE_ROW, SUMMARY, EMBEDDING, summarizer_version="r2s1-model-p1", embed_model="emb-m3")
    projected = LegifranceDbWriter.project_legacy_chunks([row])
    assert len(projected) == 1
    assert projected[0]["chunk_id"] == "LEGIARTI000044420769_r2s"
    assert projected[0]["index_variant"] == row["index_variant"]
    assert projected[0]["chunk_text"] == ARTICLE_ROW["chunk_text"]
    assert projected[0]["text"] == SUMMARY
    assert projected[0]["embedding_m3"] == EMBEDDING
    assert "chunk_text_tsv" not in projected[0]  # colonne générée, jamais écrite


# --- Plan delta (absent / périmé / à jour) ------------------------------------


def test_plan_missing_summaries_delta() -> None:
    version = "r2s1-model-p1"
    up_to_date = {**ARTICLE_ROW}
    changed = {
        **ARTICLE_ROW,
        "cid": "LEGIARTI000000000002",
        "chunk_id": "LEGIARTI000000000002_0",
        "chunk_text": "Texte modifié depuis la génération.",
    }
    missing = {**ARTICLE_ROW, "cid": "LEGIARTI000000000003", "chunk_id": "LEGIARTI000000000003_0"}
    empty = {**ARTICLE_ROW, "cid": "LEGIARTI000000000004", "chunk_id": "LEGIARTI000000000004_0", "chunk_text": "  "}

    existing = {
        # À jour: variant attendu (même version, même checksum).
        up_to_date["cid"]: sr.build_index_variant(version, up_to_date["chunk_text"], embed_model="emb-m3"),
        # Périmé: variant calculé sur l'ANCIEN texte.
        changed["cid"]: sr.build_index_variant(version, "ancien texte", embed_model="emb-m3"),
    }
    todo = sr.plan_missing_summaries([up_to_date, changed, missing, empty], existing, version, embed_model="emb-m3")
    assert [row["cid"] for row in todo] == [changed["cid"], missing["cid"]]


def test_plan_missing_summaries_is_stale_on_version_bump() -> None:
    existing = {ARTICLE_ROW["cid"]: sr.build_index_variant("r2s1-model-p1", ARTICLE_ROW["chunk_text"], embed_model="emb-m3")}
    todo = sr.plan_missing_summaries([ARTICLE_ROW], existing, "r2s2-model-p1", embed_model="emb-m3")
    assert [row["cid"] for row in todo] == [ARTICLE_ROW["cid"]]


def test_plan_missing_summaries_ignores_summary_rows_in_input() -> None:
    summary_row = {
        **ARTICLE_ROW,
        "chunk_id": "LEGIARTI000044420769_r2s",
        "index_variant": sr.build_index_variant("r2s1-model-p1", ARTICLE_ROW["chunk_text"], embed_model="emb-m3"),
    }
    todo = sr.plan_missing_summaries([summary_row], {}, "r2s1-model-p1", embed_model="emb-m3")
    assert todo == []  # une ligne R2 ne planifie jamais son propre résumé


def test_index_variant_embed_model_enters_freshness_key() -> None:
    """Revue #332 : changer le modèle d'embedding doit invalider les lignes R2
    (sinon des vecteurs d'un autre espace restent considérés « à jour »)."""
    v_m3 = sr.build_index_variant("r2s1-model-p1", "texte A", embed_model="emb-m3")
    v_new = sr.build_index_variant("r2s1-model-p1", "texte A", embed_model="emb-nouveau")
    assert v_m3 != v_new

    row = {"cid": "C1", "chunk_text": "texte A"}
    existing = {"C1": v_m3}
    # même summarizer, même texte, embedder changé -> l'article redevient TODO
    todo = sr.plan_missing_summaries([row], existing, "r2s1-model-p1", embed_model="emb-nouveau")
    assert [r["cid"] for r in todo] == ["C1"]

    with pytest.raises(ValueError):
        sr.build_index_variant("v", "t", embed_model="")


def test_split_stale_sources_partitions_fresh_deleted_changed() -> None:
    """Revue #332 : revalidation pré-upsert — un article supprimé ou modifié
    par une ingestion concurrente ne doit JAMAIS être réinséré depuis le
    snapshot de génération."""
    expected = {
        "OK": sr.source_sha("texte intact"),
        "GONE": sr.source_sha("texte supprimé"),
        "CHANGED": sr.source_sha("texte avant"),
    }
    current = {"OK": "texte intact", "CHANGED": "texte après ingestion"}
    fresh, stale = sr.split_stale_sources(expected, current)
    assert fresh == ["OK"]
    assert set(stale) == {"GONE", "CHANGED"}
    assert "supprimé" in stale["GONE"] and "modifié" in stale["CHANGED"]
