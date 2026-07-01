"""
Async feedback analyzer for the RAG V3 Clean pipeline.

Analyses negative feedback (≤3 stars) using Albert ``openweight-large``
with **full, untruncated** RAG pipeline context (131k context window).

A single LLM call receives:
  - The user question & RAG answer (complete)
  - The user's feedback (stars, reasons, comment)
  - All retrieved chunks (complete text)
  - All context items sent to the generator (complete)
  - The full prompt sent to the generator (complete)
  - Pipeline config flags (selector ON/OFF, etc.)

The system prompt dynamically adapts error categories based on which
pipeline stages were active for each run (e.g. no selector → no
selector-related categories).

Results are written to ``chat_feedbacks``:
  * ``error_category``  – one of the active categories
  * ``ai_reason``       – short human-readable explanation
  * ``ai_analyzed_at``  – timestamp
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

ALBERT_API_URL = os.getenv(
    "ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1"
).rstrip("/")
ANALYSIS_MODEL = "openweight-large"
NEGATIVE_THRESHOLD = 2  # 0-based: 0→1★ … 4→5★ ; ≤2 → 1-3★

# ---------------------------------------------------------------------------
# Error categories – base set always available
# ---------------------------------------------------------------------------
_BASE_CATEGORIES = {
    "retrieval_issue": "les bons chunks n'ont pas été trouvés par la recherche sémantique",
    "generator_hallucination": "le LLM générateur a inventé des informations absentes du contexte",
    "generator_incomplete": "la réponse est incomplète malgré un contexte suffisant",
    "generator_wrong_interpretation": "le LLM a mal interprété ou mal synthétisé le contexte",
    "missing_document": "l'information demandée n'existe tout simplement pas dans la base documentaire",
    "chunk_quality": "les chunks existent mais sont mal découpés, incomplets ou mal structurés",
    "other": "autre problème ne correspondant à aucune catégorie ci-dessus",
}

_SELECTOR_CATEGORIES = {
    "selector_misunderstanding": "le LLM Selector a reçu les bons chunks mais ne les a pas compris / mal évalués",
    "selector_wrong_priority": "le Selector a compris les chunks mais a mal priorisé (gardé les moins pertinents)",
}

ALL_VALID_CATEGORIES = list(_BASE_CATEGORIES.keys()) + list(_SELECTOR_CATEGORIES.keys())


def _build_system_prompt(selector_active: bool) -> str:
    """Build the system prompt with categories adapted to the pipeline config."""
    categories = dict(_BASE_CATEGORIES)
    if selector_active:
        categories.update(_SELECTOR_CATEGORIES)

    cat_lines = "\n".join(f"- {k} : {v}" for k, v in categories.items())

    pipeline_steps = (
        "1. **Retrieval** – recherche sémantique parallèle sur 4 tables de chunks (MATTE, Service-Public, Legifrance, RGRH)\n"
        "2. **Agrégation** – les chunks sont regroupés en sections, dédupliqués et rerankés\n"
    )
    if selector_active:
        pipeline_steps += "3. **LLM Selector** – un LLM filtre et priorise les sections pertinentes\n"
        pipeline_steps += "4. **Contexte** – les sections sélectionnées sont assemblées avec un budget de tokens\n"
        pipeline_steps += "5. **Génération** – un LLM produit la réponse finale à partir du contexte\n"
    else:
        pipeline_steps += "3. **Contexte** – les sections sont assemblées avec un budget de tokens (pas de LLM Selector)\n"
        pipeline_steps += "4. **Génération** – un LLM produit la réponse finale à partir du contexte\n"

    return f"""\
Tu es un expert en diagnostic de systèmes RAG spécialisé dans le domaine RH de la fonction publique française.

## Architecture du pipeline RAG V3 analysé

{pipeline_steps}
## Ta mission

Analyse l'interaction ci-dessous en détail. Tu reçois le contexte COMPLET du pipeline (rien n'est tronqué).
Identifie PRÉCISÉMENT l'étape du pipeline responsable du problème.

## Format de réponse

Réponds UNIQUEMENT en JSON valide :
{{
  "error_category": "<une des catégories ci-dessous>",
  "short_reason": "<1-2 phrases précises résumant le problème, en citant les sources/chunks concernés si pertinent>"
}}

## Catégories possibles

{cat_lines}

## Principes d'analyse

- Compare la QUESTION aux CHUNKS RETRIEVED : les bons documents ont-ils été trouvés ?
- Compare les CHUNKS au CONTEXTE FINAL : les bonnes sections ont-elles été conservées ?
- Compare le CONTEXTE FINAL à la RÉPONSE : le générateur a-t-il bien exploité le contexte ?
- Si l'information n'apparaît dans AUCUN chunk, c'est probablement "missing_document"
- Sois PRÉCIS : cite les sources (MATTE, Service-Public…) et les passages concernés"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _get_engine():
    from .db_helpers import create_engine_from_env
    return create_engine_from_env()


def _get_unanalyzed_feedbacks(engine, limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch negative feedbacks with full RAG context from chat_runs."""
    query = text("""
        SELECT
            f.id,
            f.turn_id,
            f.question,
            f.answer,
            f.stars,
            f.reasons_negative,
            f.comment,
            r.v3_full_prompt            AS rag_prompt,
            r.chunks_sent_to_selector   AS retrieved_chunks,
            r.v3_source_distribution    AS source_distribution,
            r.v3_sections_count,
            r.v3_context_items_count,
            r.v3_context_tokens,
            r.v3_context_mode,
            r.llm_selector_reasoning,
            r.llm_selector_response,
            r.v3_selector_confidence,
            r.v3_selector_selected_count,
            r.v3_selector_decisions,
            r.llm_selector_model
        FROM chat_feedbacks f
        LEFT JOIN chat_runs r ON r.turn_id = f.turn_id
        WHERE f.stars IS NOT NULL
          AND f.stars <= :threshold
          AND f.error_category IS NULL
        ORDER BY f.ts DESC
        LIMIT :lim
    """)
    with engine.connect() as conn:
        rows = conn.execute(query, {"threshold": NEGATIVE_THRESHOLD, "lim": limit})
        return [dict(r._mapping) for r in rows]


def _save_analysis(engine, feedback_id: int, category: str, reason: str) -> bool:
    """Write analysis results back to chat_feedbacks."""
    query = text("""
        UPDATE chat_feedbacks
        SET error_category = :cat,
            ai_reason = :reason,
            ai_analyzed_at = :ts
        WHERE id = :fid
    """)
    try:
        with engine.connect() as conn:
            conn.execute(query, {
                "cat": category,
                "reason": reason,
                "ts": datetime.now(timezone.utc),
                "fid": feedback_id,
            })
            conn.commit()
        return True
    except SQLAlchemyError as exc:
        logger.error("Failed to save analysis for feedback %s: %s", feedback_id, exc)
        return False


# ---------------------------------------------------------------------------
# On-demand content resolution from lightweight IDs
# ---------------------------------------------------------------------------

_TABLE_ID_COL = {
    "rag_chunks_matte": "hash_id",
    "rag_chunks_service_public": "hash_id",
    "rag_chunks_dgafp": "chunk_id",
    "rag_chunks_rgrh": "hash_id",
}

_ALLOWED_TABLES = set(_TABLE_ID_COL.keys())


def _resolve_chunk_content(engine, chunk_refs) -> List[Dict[str, Any]]:
    """Resolve lightweight chunk refs to full text by querying the source tables.

    *chunk_refs* is a list of ``{"chunk_id": ..., "table": ..., "score": ..., "section_id": ...}``.
    Returns enriched dicts with a ``"text"`` key added.
    """
    refs = _json_field(chunk_refs)
    if not refs or not isinstance(refs, list):
        return []

    by_table: Dict[str, List[str]] = {}
    for r in refs:
        tbl = r.get("table", "")
        if tbl in _ALLOWED_TABLES:
            by_table.setdefault(tbl, []).append(str(r["chunk_id"]))

    resolved: Dict[str, str] = {}
    try:
        with engine.connect() as conn:
            for tbl, ids in by_table.items():
                id_col = _TABLE_ID_COL[tbl]
                q = text(f"SELECT {id_col} AS cid, chunk_text FROM {tbl} WHERE {id_col} = ANY(:ids)")
                rows = conn.execute(q, {"ids": ids})
                for row in rows:
                    resolved[str(row.cid)] = row.chunk_text or ""
    except SQLAlchemyError as exc:
        logger.warning("Chunk content resolution failed: %s", exc)

    enriched = []
    for r in refs:
        entry = dict(r)
        entry["text"] = resolved.get(str(r["chunk_id"]), "")
        enriched.append(entry)
    return enriched


def _resolve_section_content(engine, section_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve section IDs to full markdown from ``rag_sections``.

    Returns ``{section_id: {"heading": ..., "markdown": ..., "doc_id": ...}}``.
    """
    if not section_ids:
        return {}
    try:
        with engine.connect() as conn:
            q = text("""
                SELECT section_id::text AS sid, heading, markdown_content, doc_id
                FROM rag_sections
                WHERE section_id::text = ANY(:ids)
            """)
            rows = conn.execute(q, {"ids": [str(s) for s in section_ids]})
            return {
                str(row.sid): {
                    "heading": row.heading or "",
                    "markdown": row.markdown_content or "",
                    "doc_id": row.doc_id or "",
                }
                for row in rows
            }
    except SQLAlchemyError as exc:
        logger.warning("Section content resolution failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Context formatting – NOTHING truncated
# ---------------------------------------------------------------------------
def _json_field(value) -> Any:
    """Parse a JSON field that might be a string, list, dict, or None."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, json.JSONDecodeError):
            return value
    return value


def _format_chunks(chunks: List[Dict[str, Any]]) -> str:
    """Format resolved chunks for the LLM – no truncation.

    Accepts both legacy full-content dicts and new resolved refs
    (with ``text`` key populated by ``_resolve_chunk_content``).
    """
    if not chunks:
        return "(Aucun chunk retrieved disponible)"

    lines = []
    for i, c in enumerate(chunks, 1):
        if isinstance(c, dict):
            src = c.get("table") or c.get("source_name") or c.get("table_source", "?")
            score = c.get("score", "?")
            section_id = c.get("section_id", "")
            chunk_text = c.get("text") or c.get("chunk_text", "")
            lines.append(f"[Chunk {i}] Source: {src} | Score: {score} | Section: {section_id}")
            if chunk_text:
                lines.append(chunk_text)
            else:
                lines.append("(contenu non résolu)")
            lines.append("")
        else:
            lines.append(f"[Chunk {i}] {str(c)}")
            lines.append("")

    return f"Total: {len(chunks)} chunks\n\n" + "\n".join(lines)


def _format_selector_info(feedback: Dict[str, Any]) -> str:
    """Format LLM Selector reasoning and decisions."""
    parts: List[str] = []

    reasoning = feedback.get("llm_selector_reasoning")
    if reasoning:
        parts.append(f"Raisonnement du Selector:\n{reasoning}")

    response = feedback.get("llm_selector_response")
    if response:
        parts.append(f"Réponse brute du Selector:\n{response}")

    decisions = _json_field(feedback.get("v3_selector_decisions"))
    if decisions and isinstance(decisions, dict):
        kept = decisions.get("kept", [])
        removed = decisions.get("removed", [])
        dec_reason = decisions.get("reason", "")
        kept_str = ", ".join(f"[{d.get('idx')}] {d.get('heading', '?')}" for d in kept if isinstance(d, dict))
        removed_str = ", ".join(f"[{d.get('idx')}] {d.get('heading', '?')}" for d in removed if isinstance(d, dict))
        parts.append(f"Décisions du Selector:\n  Gardés: {kept_str or 'aucun'}\n  Retirés: {removed_str or 'aucun'}\n  Raison: {dec_reason or 'N/A'}")

    confidence = feedback.get("v3_selector_confidence")
    selected = feedback.get("v3_selector_selected_count")
    if confidence is not None or selected is not None:
        parts.append(f"Confiance: {confidence} | Items sélectionnés: {selected}")

    return "\n\n".join(parts) if parts else ""


def _build_user_prompt(feedback: Dict[str, Any], engine=None) -> str:
    """Build the full user prompt with all available context, nothing truncated.

    When *engine* is provided, lightweight chunk/section refs stored in
    ``chat_runs`` are resolved to full text via SQL queries.
    """
    stars = feedback.get("stars", 0)
    feedback_text = f"Note: {stars + 1}/5"
    if feedback.get("reasons_negative"):
        feedback_text += f"\nRaisons: {feedback['reasons_negative']}"
    if feedback.get("comment"):
        feedback_text += f"\nCommentaire: {feedback['comment']}"

    sections = [
        f"## 1. Question utilisateur\n{feedback.get('question', '(vide)')}",
        f"## 2. Réponse générée par le RAG\n{feedback.get('answer', '(vide)')}",
        f"## 3. Feedback utilisateur (NÉGATIF)\n{feedback_text}",
    ]

    # Pipeline metadata
    meta_parts = []
    if feedback.get("v3_context_mode"):
        meta_parts.append(f"Context mode: {feedback['v3_context_mode']}")
    if feedback.get("v3_sections_count") is not None:
        meta_parts.append(f"Sections: {feedback['v3_sections_count']}")
    if feedback.get("v3_context_items_count") is not None:
        meta_parts.append(f"Context items: {feedback['v3_context_items_count']}")
    if feedback.get("v3_context_tokens") is not None:
        meta_parts.append(f"Context tokens: {feedback['v3_context_tokens']}")
    selector_active = bool(feedback.get("llm_selector_model"))
    meta_parts.append(f"LLM Selector: {'ACTIF' if selector_active else 'DÉSACTIVÉ'}")
    if meta_parts:
        sections.append("## 4. Configuration du pipeline\n" + " | ".join(meta_parts))

    # Chunks retrieved – resolve from IDs if needed
    raw_chunks = _json_field(feedback.get("retrieved_chunks"))
    if engine and raw_chunks and isinstance(raw_chunks, list):
        first = raw_chunks[0] if raw_chunks else {}
        if isinstance(first, dict) and "chunk_id" in first and "text" not in first:
            raw_chunks = _resolve_chunk_content(engine, raw_chunks)
    chunks_text = _format_chunks(raw_chunks if isinstance(raw_chunks, list) else [])
    sections.append(f"## 5. TOUS les chunks retrieved (complets)\n{chunks_text}")

    # Selector info (only if active)
    if selector_active:
        selector_text = _format_selector_info(feedback)
        if selector_text:
            sections.append(f"## 6. Analyse du LLM Selector\n{selector_text}")

    # Full generator prompt (already contains all context items)
    rag_prompt = feedback.get("rag_prompt", "")
    if rag_prompt:
        sections.append(f"## 7. Prompt COMPLET envoyé au générateur (contient tout le contexte sélectionné)\n{rag_prompt}")
    else:
        sections.append("## 7. Prompt envoyé au générateur\n(Non disponible)")

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
def _call_albert(system_prompt: str, user_prompt: str) -> Tuple[str, str]:
    """Single Albert call with full context. Returns (category, reason)."""
    api_key = os.getenv("ALBERT_API_KEY")
    if not api_key:
        return "other", "ALBERT_API_KEY non configurée"

    try:
        resp = requests.post(
            f"{ALBERT_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": ANALYSIS_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 500,
                "temperature": 0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        data = json.loads(raw)
        category = data.get("error_category", "other")
        if category not in ALL_VALID_CATEGORIES:
            category = "other"
        return category, data.get("short_reason", "Analyse effectuée")

    except Exception as exc:
        logger.warning("Albert analysis call failed: %s", exc)
        return "other", f"Erreur analyse: {exc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def analyze_single(feedback: Dict[str, Any], engine=None) -> Tuple[str, str]:
    """Analyze a single feedback with full untruncated context.

    If *engine* is provided it is used both for resolving lightweight
    chunk/section refs and for persisting the analysis result.
    """
    selector_active = bool(feedback.get("llm_selector_model"))
    system_prompt = _build_system_prompt(selector_active)
    user_prompt = _build_user_prompt(feedback, engine=engine)

    category, reason = _call_albert(system_prompt, user_prompt)

    if engine and feedback.get("id"):
        _save_analysis(engine, feedback["id"], category, reason)

    return category, reason


def run_batch_analysis(limit: int = 50) -> Dict[str, int]:
    """Analyze all unanalyzed negative feedbacks in batch.

    Returns ``{"analyzed": N, "failed": M}``.
    """
    engine = _get_engine()
    if not engine:
        return {"analyzed": 0, "failed": 0, "error": "No DB connection"}

    feedbacks = _get_unanalyzed_feedbacks(engine, limit=limit)
    stats: Dict[str, int] = {"analyzed": 0, "failed": 0}

    for fb in feedbacks:
        try:
            category, reason = analyze_single(fb, engine=engine)
            if category:
                stats["analyzed"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1

    logger.info("Batch analysis complete: %s", stats)
    return stats
