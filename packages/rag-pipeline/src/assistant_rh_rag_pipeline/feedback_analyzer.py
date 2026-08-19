"""
Async feedback analyzer for the RAG V3 Clean pipeline.

Analyses negative feedback (≤3 stars) in two stages:

1. **Attribution mécanique d'étage** — un premier appel LLM léger extrait des
   *marqueurs* (chaînes distinctives de l'information attendue), puis leur
   présence est vérifiée mécaniquement à chaque étage du pipeline : corpus
   (tables de chunks) → pool de retrieval (``v3_chunks_raw``) → entrée du
   selector → contexte du générateur (``v3_full_prompt``). L'étage de mort
   détermine la catégorie sans jugement LLM (``missing_document``,
   ``retrieval_issue``, ``candidate_cut``, ``selector_wrong_priority``).
2. **Jugement LLM plein contexte** — uniquement quand l'information était bien
   dans le contexte du générateur (familles ``generator_*`` /
   ``chunk_quality``), ou en secours si aucun marqueur exploitable.

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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import CHUNK_TABLES

logger = logging.getLogger(__name__)

ALBERT_API_URL = os.getenv("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
ANALYSIS_MODEL = "openweight-large"
NEGATIVE_THRESHOLD = 2  # 0-based: 0→1★ … 4→5★ ; ≤2 → 1-3★

# ---------------------------------------------------------------------------
# Error categories – base set always available
# ---------------------------------------------------------------------------
_BASE_CATEGORIES = {
    "retrieval_issue": "les bons chunks n'ont pas été trouvés par la recherche sémantique",
    "candidate_cut": (
        "les bons chunks ont été retrouvés par le retrieval mais éliminés avant la génération (agrégation, coupe des candidats ou budget de contexte)"
    ),
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


def _build_system_prompt(selector_active: bool, stage_hint: str = "") -> str:
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
- Sois PRÉCIS : cite les sources (MATTE, Service-Public…) et les passages concernés{_format_stage_hint(stage_hint)}"""


def _format_stage_hint(stage_hint: str) -> str:
    if not stage_hint:
        return ""
    return f"\n\n## Attribution d'étage (calculée mécaniquement — fais-lui confiance)\n\n{stage_hint}"


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
            r.llm_selector_model,
            r.selected_ministry,
            r.v3_chunks_raw,
            r.retrieved              AS served_sources
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
            conn.execute(
                query,
                {
                    "cat": category,
                    "reason": reason,
                    "ts": datetime.now(timezone.utc),
                    "fid": feedback_id,
                },
            )
            conn.commit()
        return True
    except SQLAlchemyError as exc:
        logger.error("Failed to save analysis for feedback %s: %s", feedback_id, exc)
        return False


# ---------------------------------------------------------------------------
# On-demand content resolution from lightweight IDs
# ---------------------------------------------------------------------------

_TABLE_ID_COL = {table.name: table.id_col for table in CHUNK_TABLES.values()}

_ALLOWED_TABLES = set(_TABLE_ID_COL.keys())
_TABLE_BY_ALIAS = {alias.lower(): table.name for key, table in CHUNK_TABLES.items() for alias in (key, table.name, table.publisher) if alias}
_MINISTRY_CHUNK_TABLES = {key: CHUNK_TABLES[key].name for key in ("matte", "mso", "mi", "masa")}


def _chunk_table_for_ref(ref: Dict[str, Any], selected_ministry: Optional[str]) -> str:
    """Resolve a logged chunk ref to a code-owned, allow-listed table name.

    Current traces store shared sources as canonical table names, but ministry
    sources may be stored under their full publisher label. In the latter case,
    the request's ``selected_ministry`` is the authoritative table scope.
    """
    raw_source = next(
        (ref.get(key) for key in ("table", "doc_publisher", "publisher", "table_source", "source") if ref.get(key)),
        "",
    )
    source = str(raw_source).strip()
    table = _TABLE_BY_ALIAS.get(source.lower())
    if table:
        return table

    ministry_table = _MINISTRY_CHUNK_TABLES.get((selected_ministry or "").strip().lower())
    if source and ministry_table:
        return ministry_table
    return ""


def _resolve_chunk_content(engine, chunk_refs, selected_ministry: Optional[str] = None) -> List[Dict[str, Any]]:
    """Resolve lightweight chunk refs to full text by querying the source tables.

    *chunk_refs* is a list of ``{"chunk_id": ..., "table": ..., "score": ..., "section_id": ...}``.
    Returns enriched dicts with ``"text"`` and ``"_content_resolved"`` added.
    """
    refs = _json_field(chunk_refs)
    if not refs or not isinstance(refs, list):
        return []

    by_table: Dict[str, List[str]] = {}
    for r in refs:
        if not isinstance(r, dict):
            continue
        chunk_id = r.get("chunk_id") or r.get("id")
        tbl = _chunk_table_for_ref(r, selected_ministry)
        if chunk_id and tbl in _ALLOWED_TABLES:
            by_table.setdefault(tbl, []).append(str(chunk_id))

    resolved: Dict[Tuple[str, str], str] = {}
    try:
        with engine.connect() as conn:
            for tbl, ids in by_table.items():
                id_col = _TABLE_ID_COL[tbl]
                q = text(f"SELECT {id_col} AS cid, chunk_text FROM {tbl} WHERE {id_col} = ANY(:ids)")
                rows = conn.execute(q, {"ids": ids})
                for row in rows:
                    resolved[(tbl, str(row.cid))] = row.chunk_text or ""
    except SQLAlchemyError as exc:
        logger.warning("Chunk content resolution failed: %s", exc)

    enriched = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        entry = dict(r)
        chunk_id = str(r.get("chunk_id") or r.get("id") or "")
        tbl = _chunk_table_for_ref(r, selected_ministry)
        key = (tbl, chunk_id)
        existing_text = entry.get("text") or entry.get("chunk_text")
        if existing_text:
            entry["text"] = existing_text
            entry["_content_resolved"] = True
        else:
            entry["text"] = resolved.get(key, "")
            entry["_content_resolved"] = key in resolved
        enriched.append(entry)
    return enriched


def _chunk_refs_complete(refs: Any) -> bool:
    """Whether every logged ref contains full text rather than a preview."""
    parsed = _json_field(refs)
    if not isinstance(parsed, list):
        return False
    return all(isinstance(ref, dict) and (ref.get("_content_resolved") is True or bool(ref.get("text") or ref.get("chunk_text"))) for ref in parsed)


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
            raw_chunks = _resolve_chunk_content(engine, raw_chunks, feedback.get("selected_ministry"))
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
# LLM call – robust JSON handling
# ---------------------------------------------------------------------------
def _extract_json(raw: str) -> Dict[str, Any]:
    """Extract a JSON object from an LLM response, repairing truncation.

    Handles code fences, prose around the object, an odd number of quotes
    (string cut mid-flight by ``max_tokens``) and unclosed braces.
    """
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    raw = raw.strip()
    start = raw.find("{")
    if start == -1:
        raise ValueError("aucun objet JSON dans la réponse")
    raw = raw[start:]
    end = raw.rfind("}")
    if end != -1:
        try:
            return json.loads(raw[: end + 1])
        except json.JSONDecodeError:
            pass
    repaired = raw.rstrip()
    if repaired.count('"') % 2 == 1:
        repaired += '"'
    repaired = re.sub(r",\s*$", "", repaired)
    repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
    return json.loads(repaired)


def _call_albert_json(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    retries: int = 1,
) -> Dict[str, Any]:
    """Albert call that must return a JSON object. Retries once on failure.

    Falls back to ``reasoning_content`` when the model spends its budget in
    reasoning and returns ``content: null``.
    """
    api_key = os.getenv("ALBERT_API_KEY")
    if not api_key:
        raise RuntimeError("ALBERT_API_KEY non configurée")

    last_exc: Exception = RuntimeError("appel non tenté")
    for attempt in range(retries + 1):
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
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
                timeout=90,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning_content") or ""
            if not raw.strip():
                raise ValueError("réponse vide du modèle (content=null)")
            return _extract_json(raw)
        except Exception as exc:  # noqa: BLE001 – on retente puis on remonte
            last_exc = exc
            logger.warning(
                "Albert JSON call failed (tentative %d/%d): %s",
                attempt + 1,
                retries + 1,
                exc,
            )
    raise RuntimeError(str(last_exc))


def _call_albert(system_prompt: str, user_prompt: str) -> Tuple[str, str]:
    """Single Albert call with full context. Returns (category, reason)."""
    data = _call_albert_json(system_prompt, user_prompt)
    category = data.get("error_category", "other")
    if category not in ALL_VALID_CATEGORIES:
        raise ValueError(f"catégorie Albert invalide: {category!r}")
    return category, data.get("short_reason", "Analyse effectuée")


# ---------------------------------------------------------------------------
# Mechanical stage attribution
# ---------------------------------------------------------------------------
_MARKER_SYSTEM_PROMPT = """\
Tu es un expert RH de la fonction publique française. On te donne une question posée
à un assistant documentaire, la réponse produite et le feedback négatif d'un testeur.

Ta mission : identifier l'information qui manquait (ou était fausse) et produire des
MARQUEURS de recherche — de courtes chaînes qui apparaîtraient telles quelles dans un
document source contenant cette information.

Règles pour les marqueurs :
- 2 à 6 marqueurs, chacun de 4 à 40 caractères, de 1 à 3 mots maximum
- très spécifiques : valeur chiffrée (« 159,20 »), terme technique (« plafond d'emploi »), référence (« annexe 3 »), intitulé précis
- en français, avec les accents, sans guillemets ni ponctuation d'encadrement
- PAS de mots génériques (« contrat », « agent », « ministère »)
- N'INVENTE JAMAIS de référence (numéro de décret, d'article…) : n'utilise une
  référence que si elle apparaît dans la question, la réponse ou le commentaire

Réponds UNIQUEMENT en JSON valide :
{"missing_info": "<1 phrase : l'information attendue>", "markers": ["...", "..."]}"""

_SHARED_CHUNK_TABLES = ["rag_chunks_service_public", "rag_chunks_dgafp"]


_DASHES = "‐‑‒–—―−"


def _normalize(s: str) -> str:
    out = (s or "").lower().replace("’", "'").replace("\u00a0", " ")
    for dash in _DASHES:
        out = out.replace(dash, "-")
    return out


# Références numérotées (« décret n° 2020-1234 », « article L113-3 ») : mode
# d'hallucination dominant du LLM extracteur. Une référence n'est conservée
# que si elle apparaît verbatim dans la conversation (question/réponse/feedback).
_REF_MARKER_PATTERN = re.compile(r"(décret|arrêté|article|circulaire|instruction|loi)\b.*\d", re.IGNORECASE)


def _filter_hallucinated_markers(markers: List[str], feedback: Dict[str, Any]) -> List[str]:
    grounding = _normalize(" ".join(str(feedback.get(k) or "") for k in ("question", "answer", "comment", "reasons_negative")))
    return [m for m in markers if not _REF_MARKER_PATTERN.search(m) or _normalize(m) in grounding]


def _extract_markers(feedback: Dict[str, Any]) -> Tuple[str, List[str]]:
    """LLM stage 1: what information was expected, and how to search for it."""
    stars = int(feedback.get("stars") or 0)
    user = (
        f"## Question\n{feedback.get('question', '')}\n\n"
        f"## Réponse de l'assistant\n{feedback.get('answer', '')}\n\n"
        f"## Feedback du testeur\nNote: {stars + 1}/5\n"
        f"Raisons: {feedback.get('reasons_negative') or '—'}\n"
        f"Commentaire: {feedback.get('comment') or '—'}"
    )
    data = _call_albert_json(_MARKER_SYSTEM_PROMPT, user, max_tokens=900)
    markers = [m.strip() for m in data.get("markers", []) if isinstance(m, str)]
    markers = [m for m in markers if 4 <= len(m) <= 60]
    markers = _filter_hallucinated_markers(markers, feedback)
    unique_markers = []
    seen = set()
    for marker in markers:
        normalized = _normalize(marker)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_markers.append(marker)
        if len(unique_markers) == 6:
            break
    return str(data.get("missing_info", "")), unique_markers


def _search_corpus(engine, ministry: Optional[str], markers: List[str]) -> Optional[bool]:
    """True/False: is any marker present in the corpus tables of this scope?

    Returns ``None`` when the ministry scope is unknown or the check could not
    run (DB error) — callers must then avoid any mechanical corpus conclusion.
    """
    ministry_table = _MINISTRY_CHUNK_TABLES.get((ministry or "").strip().lower())
    if not ministry_table:
        return None
    tables = [ministry_table, *_SHARED_CHUNK_TABLES]
    try:
        with engine.connect() as conn:
            for marker in markers:
                ascii_dashes = marker
                for dash in _DASHES:
                    ascii_dashes = ascii_dashes.replace(dash, "-")
                variants = {
                    marker,
                    ascii_dashes,
                    marker.replace("'", "’"),
                    marker.replace("’", "'"),
                    ascii_dashes.replace("’", "'"),
                }
                for tbl in tables:
                    for v in variants:
                        q = text(f"SELECT 1 FROM {tbl} WHERE chunk_text ILIKE :p LIMIT 1")
                        if conn.execute(q, {"p": f"%{v}%"}).first():
                            return True
        return False
    except SQLAlchemyError as exc:
        logger.warning("Corpus marker search failed: %s", exc)
        return None


_GENERATOR_QUESTION_HEADING = "**Question de l'utilisateur :**"


def _generator_context_from_prompt(prompt: Any) -> Optional[str]:
    """Extract only the documentary context from a logged generator prompt."""
    if not isinstance(prompt, str):
        return None
    if prompt.count(_GENERATOR_QUESTION_HEADING) != 1:
        return None
    context, _question = prompt.split(_GENERATOR_QUESTION_HEADING, 1)
    if not context.strip():
        return None
    return context


def _stage_flags(feedback: Dict[str, Any], markers: List[str]) -> Dict[str, bool]:
    """Marker presence at each pipeline stage (ANY-marker semantics)."""
    selector_input = feedback.get("retrieved_chunks")
    if not isinstance(selector_input, str):
        try:
            selector_input = json.dumps(selector_input, ensure_ascii=False)
        except (TypeError, ValueError):
            selector_input = str(selector_input)
    stages = {
        "pool": str(feedback.get("v3_chunks_raw") or ""),
        "selector_input": selector_input or "",
        "served": str(feedback.get("served_sources") or ""),
        "generator_context": _generator_context_from_prompt(feedback.get("rag_prompt")) or "",
    }
    norm_markers = [_normalize(m) for m in markers]
    return {stage: any(m in _normalize(content) for m in norm_markers) for stage, content in stages.items()}


def _classify_from_flags(
    in_corpus: Optional[bool],
    flags: Dict[str, bool],
    selector_active: bool,
    markers: List[str],
    missing_info: str,
    *,
    pool_complete: bool = True,
    selector_input_complete: bool = True,
    generator_context_complete: bool = True,
) -> Tuple[Optional[str], str]:
    """Decision tree on mechanical flags. ``None`` → generator family, LLM juge.

    The decision only uses text-bearing stages (pool, selector input,
    generator prompt) — ``served`` stores metadata and would create false
    positives on document titles.
    """
    prefix = f"Attribution mécanique (marqueurs : {', '.join(markers)}) : "
    suffix = f" Information attendue : {missing_info}" if missing_info else ""
    if in_corpus is None:
        return None, (
            "Le périmètre corpus n'a pas pu être vérifié intégralement ; ne conclus pas mécaniquement à missing_document ou retrieval_issue."
        )
    if not in_corpus:
        if len(markers) < 2:
            # Conclusion d'absence trop fragile sur un marqueur unique :
            # on délègue au LLM avec l'hypothèse à vérifier.
            return None, (
                "Le seul marqueur exploitable est introuvable dans le corpus — "
                "l'hypothèse missing_document est plausible mais non confirmée "
                "mécaniquement ; vérifie-la en priorité."
            )
        return "missing_document", (prefix + "aucun marqueur trouvé dans le corpus (table ministère + service_public + dgafp)." + suffix)
    if not flags["pool"]:
        if not pool_complete:
            return None, ("Le pool de retrieval n'a pas pu être résolu intégralement ; ne conclus pas mécaniquement à retrieval_issue.")
        return "retrieval_issue", (prefix + "présents dans le corpus mais absents du pool de retrieval." + suffix)
    if not flags["generator_context"]:
        if not generator_context_complete:
            return None, (
                "Le prompt du générateur n'est pas disponible dans la trace ; ne conclus pas mécaniquement sur l'étage ayant éliminé l'information."
            )
        if selector_active and flags["selector_input"]:
            return "selector_wrong_priority", (prefix + "retrouvés et transmis au selector, mais écartés du contexte de génération." + suffix)
        if selector_active and not selector_input_complete:
            return None, (
                "L'entrée du selector n'a pas pu être résolue intégralement ; "
                "ne tranche pas mécaniquement entre candidate_cut et selector_wrong_priority."
            )
        return "candidate_cut", (prefix + "retrouvés par le retrieval mais éliminés avant la sélection (agrégation / coupe des candidats)." + suffix)
    return None, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def analyze_single(feedback: Dict[str, Any], engine=None) -> Tuple[str, str]:
    """Analyze a single feedback: mechanical stage attribution first, LLM after.

    If *engine* is provided it is used to resolve lightweight chunk refs,
    search the corpus for markers and persist the analysis result.
    """
    feedback = dict(feedback)

    selected_ministry = feedback.get("selected_ministry")

    # Resolve selector-input chunk refs once (shared by flags + LLM prompt)
    raw_chunks = _json_field(feedback.get("retrieved_chunks"))
    if engine and isinstance(raw_chunks, list) and raw_chunks:
        raw_chunks = _resolve_chunk_content(engine, raw_chunks, selected_ministry)
    if isinstance(raw_chunks, list):
        feedback["retrieved_chunks"] = raw_chunks

    # ``v3_chunks_raw`` only logs 300-character previews. Resolve the IDs to
    # full chunk text before using absence as mechanical evidence.
    raw_pool = _json_field(feedback.get("v3_chunks_raw"))
    if engine and isinstance(raw_pool, list) and raw_pool:
        raw_pool = _resolve_chunk_content(engine, raw_pool, selected_ministry)
    if isinstance(raw_pool, list):
        feedback["v3_chunks_raw"] = raw_pool

    pool_complete = _chunk_refs_complete(feedback.get("v3_chunks_raw"))
    selector_input_complete = _chunk_refs_complete(feedback.get("retrieved_chunks"))
    rag_prompt = feedback.get("rag_prompt")
    generator_context_complete = _generator_context_from_prompt(rag_prompt) is not None

    selector_active = bool(feedback.get("llm_selector_model"))
    category: Optional[str] = None
    reason = ""
    stage_hint = ""

    # Étage 1 : attribution mécanique guidée par marqueurs
    try:
        missing_info, markers = _extract_markers(feedback)
    except Exception as exc:  # noqa: BLE001 – fallback sur l'analyse LLM seule
        logger.warning("Marker extraction failed: %s", exc)
        missing_info, markers = "", []

    if markers:
        flags = _stage_flags(feedback, markers)
        in_corpus = None
        if engine:
            in_corpus = _search_corpus(engine, feedback.get("selected_ministry"), markers)
        category, reason = _classify_from_flags(
            in_corpus,
            flags,
            selector_active,
            markers,
            missing_info,
            pool_complete=pool_complete,
            selector_input_complete=selector_input_complete,
            generator_context_complete=generator_context_complete,
        )
        if category is None:
            if reason:
                stage_hint = reason
            elif flags["generator_context"]:
                stage_hint = (
                    f"Les marqueurs de l'information attendue ({', '.join(markers)}) sont "
                    "PRÉSENTS dans le contexte envoyé au générateur. La cause est donc en "
                    "aval : choisis une catégorie generator_* ou chunk_quality."
                )

    # Étage 2 : LLM plein contexte (famille générateur, ou secours sans marqueurs)
    if category is None:
        system_prompt = _build_system_prompt(selector_active, stage_hint=stage_hint)
        user_prompt = _build_user_prompt(feedback, engine=engine)
        category, reason = _call_albert(system_prompt, user_prompt)

    if engine and feedback.get("id"):
        if not _save_analysis(engine, feedback["id"], category, reason):
            raise RuntimeError(f"échec de persistance de l'analyse du feedback {feedback['id']}")

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
            logger.exception("Feedback analysis failed for feedback %s", fb.get("id"))
            stats["failed"] += 1

    logger.info("Batch analysis complete: %s", stats)
    return stats
