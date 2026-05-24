"""
Chatbot Logging - Turn dataclass et fonctions de logging.

Extrait de 01_Chatbot.py pour plus de lisibilité.
"""

import csv
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from typing import TYPE_CHECKING

import streamlit as st
from assistant_rh_rag_pipeline.db_helpers import create_engine_from_env
from filelock import FileLock
from sqlalchemy import text

if TYPE_CHECKING:
    from assistant_rh_rag_pipeline.models import Chunk


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Turn:
    """Représente un tour de conversation (question + réponse)."""
    user: str
    assistant: str
    retrieved: List["Chunk"]
    prompt_used: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    feedback: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════════════════════════════════════
# PATHS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

BASE = Path(__file__).resolve().parent.parent.parent / "data"
RUNS_CSV = BASE / "chat_runs" / "chat_runs.csv"
FEEDS_CSV = BASE / "chat_feedbacks" / "chat_feedbacks.csv"
REVIEWS_CSV = BASE / "chat_reviews" / "reviewed.csv"

# Créer les dossiers si nécessaire
for p in [RUNS_CSV.parent, FEEDS_CSV.parent, REVIEWS_CSV.parent]:
    p.mkdir(parents=True, exist_ok=True)

RUNS_FIELDS = [
    "ts", "turn_id", "question", "answer", "provider", "model", "temperature",
    "backend", "table", "embed_col", "filters", "top_k", "use_reranker",
    "reranker_name", "rerank_top_k", "retrieved", "prompt", "system_prompt", "system_prompt_name",
    "use_query_rewriting", "rewritten_query", "use_hyde", "hyde_document",
    "use_query_expansion", "expanded_query", "acronyms_used",
    "retrieval_mode", "hybrid_alpha", "sparse_method",
    "session_id", "conversation_id", "turn_index",
    "sources_used_count", "sources_used_indices", "sources_used_content", "fallbacks_used", "sources_raw_line",
    "retrieval_time_ms", "rerank_time_ms", "llm_time_ms", "total_time_ms",
    "ttft_ms", "tokens_per_second",
    "dist_before_rerank", "dist_after_rerank",
    "boost_weights",
    "use_intent_gating", "intent_result", "intent_confidence", "intent_model",
    "use_query_reformulation", "reformulated_query", "reformulation_model",
    "pipeline_latency_ms", "direct_response",
    # RAG V2 specific fields
    "rag_version", "chunk_selection_mode", "cascade_source", "expanded_refs_count", "user_group",
    # LLM Selector tracking
    "llm_selector_model", "llm_selector_prompt_name", "llm_selector_reasoning", "llm_selector_time_ms",
    # PickMode tracking
    "pick_mode", "chunks_before_pick", "chunks_after_pick",
    # Intent Gating prompt
    "intent_gating_prompt_name",
]

FEEDS_FIELDS = [
    "ts", "turn_id", "turn_idx", "helpful", "reasons", "comment", "stars",
    "reasons_positive", "reasons_negative", "session_id", "question", "answer"
]

REVIEWS_FIELDS = ["turn_id", "reviewed", "question", "answer", "notes", "ts"]


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE ENGINE (CACHED)
# ═══════════════════════════════════════════════════════════════════════════════

_engine_cache = None

def get_engine():
    """Get SQLAlchemy engine with caching."""
    global _engine_cache
    
    if _engine_cache is not None:
        return _engine_cache

    try:
        engine = create_engine_from_env()
        if engine is None:
            return None
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _engine_cache = engine
        return engine
    except Exception as e:
        if "timed out" in str(e).lower() or "connection" in str(e).lower():
            return None
        else:
            st.warning(f"PostgreSQL non disponible (fallback CSV): {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# CSV FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

def _append_csv_row(path: Path, fieldnames: list, row: dict):
    """Fallback CSV logging if PostgreSQL is unavailable."""
    row = {k: row.get(k, "") for k in fieldnames}
    lock = FileLock(str(path) + ".lock")
    with lock:
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                w.writeheader()
            w.writerow(row)


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def log_run_row(row: dict):
    """Log a chat run to PostgreSQL (or CSV fallback)."""
    engine = get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                data = row.copy()
                
                # Convert to JSON strings
                if isinstance(data.get("filters"), dict):
                    data["filters"] = json.dumps(data["filters"])
                elif not data.get("filters"):
                    data["filters"] = "{}"
                
                if isinstance(data.get("retrieved"), (list, dict)):
                    data["retrieved"] = json.dumps(data["retrieved"])
                elif not data.get("retrieved"):
                    data["retrieved"] = "[]"
                
                if isinstance(data.get("sources_used_content"), (list, dict)):
                    data["sources_used_content"] = json.dumps(data["sources_used_content"], ensure_ascii=False)
                elif not data.get("sources_used_content"):
                    data["sources_used_content"] = "[]"
                
                if isinstance(data.get("boost_weights"), dict):
                    data["boost_weights"] = json.dumps(data["boost_weights"], ensure_ascii=False)
                elif not data.get("boost_weights"):
                    data["boost_weights"] = "{}"
                
                conn.execute(text("""
                    INSERT INTO chat_runs (
                        ts, turn_id, question, answer, provider, model, temperature,
                        backend, "table", embed_col, filters, top_k, use_reranker,
                        reranker_name, rerank_top_k, retrieved, prompt, system_prompt, system_prompt_name,
                        use_query_rewriting, rewritten_query, use_hyde, hyde_document,
                        use_query_expansion, expanded_query, acronyms_used,
                        retrieval_mode, hybrid_alpha, sparse_method,
                        session_id, conversation_id, turn_index,
                        sources_used_count, sources_used_indices, sources_used_content, fallbacks_used, sources_raw_line,
                        retrieval_time_ms, rerank_time_ms, llm_time_ms, total_time_ms,
                        ttft_ms, tokens_per_second,
                        dist_before_rerank, dist_after_rerank,
                        boost_weights,
                        use_intent_gating, intent_result, intent_confidence, intent_model,
                        use_query_reformulation, reformulated_query, reformulation_model,
                        pipeline_latency_ms, direct_response,
                        rag_version, chunk_selection_mode, cascade_source, expanded_refs_count, user_group,
                        llm_selector_model, llm_selector_prompt_name, llm_selector_reasoning, llm_selector_time_ms,
                        pick_mode, chunks_before_pick, chunks_after_pick,
                        intent_gating_prompt_name
                    ) VALUES (
                        :ts, :turn_id, :question, :answer, :provider, :model, :temperature,
                        :backend, :table, :embed_col, CAST(:filters AS jsonb), :top_k, :use_reranker,
                        :reranker_name, :rerank_top_k, CAST(:retrieved AS jsonb), :prompt, :system_prompt, :system_prompt_name,
                        :use_query_rewriting, :rewritten_query, :use_hyde, :hyde_document,
                        :use_query_expansion, :expanded_query, :acronyms_used,
                        :retrieval_mode, :hybrid_alpha, :sparse_method,
                        :session_id, :conversation_id, :turn_index,
                        :sources_used_count, :sources_used_indices, CAST(:sources_used_content AS jsonb), :fallbacks_used, :sources_raw_line,
                        :retrieval_time_ms, :rerank_time_ms, :llm_time_ms, :total_time_ms,
                        :ttft_ms, :tokens_per_second,
                        CAST(:dist_before_rerank AS jsonb), CAST(:dist_after_rerank AS jsonb),
                        CAST(:boost_weights AS jsonb),
                        :use_intent_gating, :intent_result, :intent_confidence, :intent_model,
                        :use_query_reformulation, :reformulated_query, :reformulation_model,
                        :pipeline_latency_ms, :direct_response,
                        :rag_version, :chunk_selection_mode, :cascade_source, :expanded_refs_count, :user_group,
                        :llm_selector_model, :llm_selector_prompt_name, :llm_selector_reasoning, :llm_selector_time_ms,
                        :pick_mode, :chunks_before_pick, :chunks_after_pick,
                        :intent_gating_prompt_name
                    )
                    ON CONFLICT (turn_id) DO UPDATE SET
                        question = EXCLUDED.question,
                        answer = EXCLUDED.answer,
                        provider = EXCLUDED.provider,
                        model = EXCLUDED.model,
                        temperature = EXCLUDED.temperature,
                        retrieved = EXCLUDED.retrieved,
                        prompt = EXCLUDED.prompt,
                        system_prompt = EXCLUDED.system_prompt,
                        system_prompt_name = EXCLUDED.system_prompt_name,
                        sources_used_count = EXCLUDED.sources_used_count,
                        sources_used_indices = EXCLUDED.sources_used_indices,
                        sources_used_content = EXCLUDED.sources_used_content,
                        fallbacks_used = EXCLUDED.fallbacks_used,
                        sources_raw_line = EXCLUDED.sources_raw_line,
                        retrieval_time_ms = EXCLUDED.retrieval_time_ms,
                        rerank_time_ms = EXCLUDED.rerank_time_ms,
                        llm_time_ms = EXCLUDED.llm_time_ms,
                        total_time_ms = EXCLUDED.total_time_ms,
                        ttft_ms = EXCLUDED.ttft_ms,
                        tokens_per_second = EXCLUDED.tokens_per_second,
                        dist_before_rerank = EXCLUDED.dist_before_rerank,
                        dist_after_rerank = EXCLUDED.dist_after_rerank,
                        boost_weights = EXCLUDED.boost_weights,
                        use_intent_gating = EXCLUDED.use_intent_gating,
                        intent_result = EXCLUDED.intent_result,
                        intent_confidence = EXCLUDED.intent_confidence,
                        intent_model = EXCLUDED.intent_model,
                        use_query_reformulation = EXCLUDED.use_query_reformulation,
                        reformulated_query = EXCLUDED.reformulated_query,
                        reformulation_model = EXCLUDED.reformulation_model,
                        pipeline_latency_ms = EXCLUDED.pipeline_latency_ms,
                        direct_response = EXCLUDED.direct_response,
                        rag_version = EXCLUDED.rag_version,
                        chunk_selection_mode = EXCLUDED.chunk_selection_mode,
                        cascade_source = EXCLUDED.cascade_source,
                        expanded_refs_count = EXCLUDED.expanded_refs_count,
                        user_group = EXCLUDED.user_group,
                        llm_selector_model = EXCLUDED.llm_selector_model,
                        llm_selector_prompt_name = EXCLUDED.llm_selector_prompt_name,
                        llm_selector_reasoning = EXCLUDED.llm_selector_reasoning,
                        llm_selector_time_ms = EXCLUDED.llm_selector_time_ms,
                        pick_mode = EXCLUDED.pick_mode,
                        chunks_before_pick = EXCLUDED.chunks_before_pick,
                        chunks_after_pick = EXCLUDED.chunks_after_pick,
                        intent_gating_prompt_name = EXCLUDED.intent_gating_prompt_name
                """), data)
                conn.commit()
            return
        except Exception as e:
            # Log silently to console instead of showing to user
            print(f"⚠️ PostgreSQL log failed, using CSV fallback: {e}")
    
    # Fallback to CSV
    _append_csv_row(RUNS_CSV, RUNS_FIELDS, row)


def _auto_enrich_goldset(engine, feedback_row: dict):
    """Auto-add evaluated question to goldset_questions_v2 (best-effort)."""
    try:
        turn_id = feedback_row.get("turn_id")
        question = feedback_row.get("question")
        if not turn_id or not question:
            return

        theme = None
        try:
            with engine.connect() as conn:
                result = conn.execute(text(
                    "SELECT v3_detected_theme FROM chat_runs WHERE turn_id = :turn_id"
                ), {"turn_id": turn_id})
                row = result.fetchone()
                if row and row[0]:
                    theme = row[0]
        except Exception:
            pass

        from src.goldset import add_question_to_goldset
        add_question_to_goldset(
            engine=engine, question=question, turn_id=turn_id,
            theme=theme, goldset_name="beta_evaluated", source="user",
        )
    except Exception as e:
        logger.warning("Goldset enrichment failed: %s", e)


def log_feedback_row(row: dict):
    """Log user feedback to PostgreSQL (or CSV fallback).

    Also auto-enriches the goldset with evaluated questions.
    """
    engine = get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO chat_feedbacks (
                        ts, turn_id, turn_idx, helpful, reasons, comment, stars,
                        reasons_positive, reasons_negative, session_id, question, answer
                    ) VALUES (
                        :ts, :turn_id, :turn_idx, :helpful, :reasons, :comment, :stars,
                        :reasons_positive, :reasons_negative, :session_id, :question, :answer
                    )
                """), row)
                conn.commit()
            
            _auto_enrich_goldset(engine, row)
            return
        except Exception as e:
            st.error(f"❌ Erreur PostgreSQL (fallback CSV): {e}")
            _append_csv_row(FEEDS_CSV, FEEDS_FIELDS, row)
    else:
        _append_csv_row(FEEDS_CSV, FEEDS_FIELDS, row)


def upsert_reviews(turn_ids: list, reviewed: bool):
    """Update/insert reviews in PostgreSQL (or CSV fallback)."""
    engine = get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                for tid in turn_ids:
                    conn.execute(text("""
                        INSERT INTO chat_reviews (turn_id, reviewed, ts, updated_at)
                        VALUES (:turn_id, :reviewed, NOW(), NOW())
                        ON CONFLICT (turn_id) DO UPDATE SET
                            reviewed = EXCLUDED.reviewed,
                            updated_at = NOW()
                    """), {"turn_id": tid, "reviewed": reviewed})
                conn.commit()
            return
        except Exception as e:
            st.warning(f"PostgreSQL review update failed, using CSV fallback: {e}")
    
    # Fallback to CSV
    lock = FileLock(str(REVIEWS_CSV) + ".lock")
    with lock:
        existing = {}
        existing_notes = {}
        existing_questions = {}
        existing_answers = {}
        if REVIEWS_CSV.exists():
            with REVIEWS_CSV.open("r", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    existing[r["turn_id"]] = (r["reviewed"] == "True")
                    existing_notes[r["turn_id"]] = r.get("notes", "")
                    existing_questions[r["turn_id"]] = r.get("question", "")
                    existing_answers[r["turn_id"]] = r.get("answer", "")
        
        for tid in turn_ids:
            existing[tid] = reviewed
        
        with REVIEWS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=REVIEWS_FIELDS)
            w.writeheader()
            now = dt.datetime.now(dt.UTC).isoformat()
            for tid, val in existing.items():
                w.writerow({
                    "turn_id": tid,
                    "reviewed": bool(val),
                    "question": existing_questions.get(tid, ""),
                    "answer": existing_answers.get(tid, ""),
                    "notes": existing_notes.get(tid, ""),
                    "ts": now
                })


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def turn_index_by_id(turn_id: str) -> Optional[int]:
    """Find turn index in session state by ID."""
    for i, t in enumerate(st.session_state.get("turns", [])):
        if getattr(t, "id", None) == turn_id:
            return i
    return None


def is_feedback_pending() -> bool:
    """Check if there's pending feedback to collect."""
    turns = st.session_state.get("turns", [])
    if not turns:
        return False
    last_turn = turns[-1]
    return last_turn.feedback is None


def safe_round(x, ndigits: int = 2):
    """Safe rounding that handles None/invalid values."""
    try:
        return round(float(x), ndigits)
    except Exception:
        return None


def annotate_original_order(chunks: List["Chunk"]) -> None:
    """Stamp original rank & retrieval_score for later comparison."""
    for idx, c in enumerate(chunks, start=1):
        if c.metadata is None:
            c.metadata = {}
        c.metadata.setdefault("orig_rank", idx)
        c.metadata.setdefault("retrieval_score", c.score)
