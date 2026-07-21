"""Lignes d'index ADDITIVES R2 pour ``rag_chunks_dgafp`` (résumés d'article).

Design arrêté le 21/07/2026 (cf. revue stratégies qualité RAG §2.3) : une
ligne supplémentaire par article dont

- ``embedding_m3`` est calculé sur le **résumé métier** (le levier de
  retrieval — fossé de vocabulaire question-métier ↔ texte juridique) ;
- ``chunk_text`` reste le texte juridique **AUTHENTIQUE** de l'article
  (identique à la ligne ``{cid}_0``) : le pipeline sert le texte source sans
  AUCUN changement runtime — « le résumé TROUVE, il ne DIT jamais » est
  structurel ;
- ``text`` porte le résumé lui-même (trace auditable de ce que le vecteur
  encode, même convention brute/servi que les chunks normaux) ;
- ``index_variant`` (colonne additive, NULL sur les lignes normales) marque la
  ligne et porte la clé de fraîcheur ``r2_summary/{version}/{sha16(source)}``.

Delta / idempotence :
- upsert sur ``chunk_id = {cid}_r2s`` (stable) -> re-générer un article
  remplace sa ligne, jamais de doublon ;
- la ré-ingestion d'un article changé purge ses chunks par ``cid``
  (``_ingest_bundle_tx``), ligne R2 comprise -> ``plan_missing_summaries``
  la re-détecte par comparaison de l'``index_variant`` attendu (version de la
  logique + checksum du texte courant) au stocké ;
- ⚠️ les lignes R2 doivent TOUJOURS être insérées avec leur embedding : le
  backfill générique (``jobs/embeddings_backfill.py``) ré-embedde les lignes
  ``embedding_m3 IS NULL`` depuis ``chunk_text`` (texte authentique), ce qui
  effacerait le levier.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..utils.article_summary import R2_LOGIC_VERSION, source_checksum
from ..utils.helpers import utc_now_iso

# Suffixe du chunk_id des lignes-résumé : ``LEGIARTI…_r2s`` (24 chars, dans la
# borne VARCHAR(64)). Le préfixe cid rattache la ligne à son article pour la
# purge par cid du delta d'ingestion.
SUMMARY_CHUNK_SUFFIX = "_r2s"

# Préfixe du marqueur : filtre SQL (`index_variant LIKE 'r2_summary/%'`) et
# rollback intégral (`DELETE ... WHERE index_variant LIKE 'r2_summary/%'`).
INDEX_VARIANT_PREFIX = "r2_summary"

# Colonnes métier copiées telles quelles de la ligne article -> la ligne-résumé
# est indiscernable au serving (pills sources, lookups par number/cid).
_COPIED_COLUMNS = (
    "cid",
    "chunk_text",
    "number",
    "title",
    "full_title",
    "subtitles",
    "nota",
    "status",
    "category",
    "source_name",
    "ministry",
    "url",
    "section_parent_cid",
    "section_parent_titre",
    "lien_citations",
    "lien_citations_count",
    "lien_modifications",
    "lien_modifications_count",
    "lien_concordes",
    "lien_concordes_count",
    "comporte_liens_sp",
    "start_date",
    "end_date",
)


def summary_chunk_id(cid: str) -> str:
    return f"{str(cid).strip()}{SUMMARY_CHUNK_SUFFIX}"


def build_index_variant(summarizer_version: str, source_text: str) -> str:
    """Marqueur + clé de fraîcheur d'une ligne R2.

    ``r2_summary/{version du summarizer}/{sha16 du texte source}`` : un
    changement de logique (R2_LOGIC_VERSION), de modèle, de prompt OU du texte
    de l'article produit une valeur différente -> la ligne stockée est
    détectée périmée par ``plan_missing_summaries``.
    """
    digest = hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()[:16]
    return f"{INDEX_VARIANT_PREFIX}/{summarizer_version}/{digest}"


def is_summary_variant(index_variant: Any) -> bool:
    return str(index_variant or "").startswith(f"{INDEX_VARIANT_PREFIX}/")


def plan_missing_summaries(
    article_rows: list[dict[str, Any]],
    existing_variants: dict[str, str],
    summarizer_version: str,
) -> list[dict[str, Any]]:
    """Articles dont la ligne R2 est absente OU périmée (delta par checksum).

    ``article_rows`` : lignes-article de ``rag_chunks_dgafp`` (au moins
    ``cid`` + ``chunk_text`` ; les lignes R2 — ``index_variant`` renseigné —
    sont ignorées si présentes). ``existing_variants`` : ``cid`` ->
    ``index_variant`` stocké des lignes R2 déjà en base. Idempotent : un
    article à jour (même version + même checksum) n'est jamais re-généré.
    """
    todo: list[dict[str, Any]] = []
    for row in article_rows:
        if is_summary_variant(row.get("index_variant")):
            continue
        cid = str(row.get("cid") or "").strip()
        source_text = str(row.get("chunk_text") or "")
        if not cid or not source_text.strip():
            continue
        expected = build_index_variant(summarizer_version, source_text)
        if existing_variants.get(cid) == expected:
            continue
        todo.append(row)
    return todo


def build_summary_chunk_row(
    article_row: dict[str, Any],
    summary: str,
    embedding_m3: list[float],
    *,
    summarizer_version: str,
) -> dict[str, Any]:
    """Ligne additive R2 prête pour ``LegifranceDbWriter.upsert_legacy_chunks``.

    L'embedding est OBLIGATOIRE (jamais de ligne R2 sans vecteur, cf. piège
    backfill en tête de module) et doit avoir été calculé sur ``summary``,
    pas sur le texte authentique.
    """
    cid = str(article_row.get("cid") or "").strip()
    source_text = str(article_row.get("chunk_text") or "")
    if not cid:
        raise ValueError("ligne article sans cid: impossible de construire la ligne-résumé")
    if not str(summary or "").strip():
        raise ValueError(f"résumé vide pour {cid}: ligne R2 refusée")
    if not embedding_m3:
        raise ValueError(f"embedding manquant pour la ligne R2 de {cid} (piège backfill: jamais de ligne R2 sans vecteur)")

    now = utc_now_iso()
    row: dict[str, Any] = {column: article_row.get(column) for column in _COPIED_COLUMNS}
    row.update(
        {
            "chunk_id": summary_chunk_id(cid),
            # Le résumé est la matière BRUTE de l'index (ce que le vecteur
            # encode) ; chunk_text (copié ci-dessus) reste le texte servi.
            "text": str(summary).strip(),
            "index_variant": build_index_variant(summarizer_version, source_text),
            # Pas une position dans le découpage de l'article.
            "chunk_number": None,
            "embedding_m3": embedding_m3,
            "created_at": now,
            "updated_at": now,
            "_targets": ["legacy"],
        }
    )
    return row


def summary_row_checksum(article_row: dict[str, Any]) -> str:
    """Checksum du texte source (clé de cache résumé) d'une ligne article."""
    return source_checksum(str(article_row.get("chunk_text") or ""))


__all__ = [
    "INDEX_VARIANT_PREFIX",
    "R2_LOGIC_VERSION",
    "SUMMARY_CHUNK_SUFFIX",
    "build_index_variant",
    "build_summary_chunk_row",
    "is_summary_variant",
    "plan_missing_summaries",
    "summary_chunk_id",
    "summary_row_checksum",
]
