from __future__ import annotations

import os

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SUPPRESS WARNINGS - Must be FIRST before any streamlit import
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import warnings

# Suppress st.cache deprecation warning from streamlit-cookies-manager
warnings.filterwarnings("ignore", message=".*st.cache.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*st.cache.*", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*cache.*deprecated.*")
warnings.filterwarnings("ignore", message=".*use_container_width.*")

import streamlit as st  # noqa: E402 – early import for monkey-patch

if not hasattr(st, "_original_cache"):
    st._original_cache = getattr(st, "cache", None)

    def _compat_cache(func=None, **_kwargs):
        """Drop-in for @st.cache that forwards to st.cache_resource (ignores legacy kwargs)."""
        return st.cache_resource(func)

    st.cache = _compat_cache

import csv
import datetime as dt
import json
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Load environment variables from .env
from dotenv import load_dotenv
from filelock import FileLock
from sqlalchemy import text
from streamlit_cookies_manager import EncryptedCookieManager

load_dotenv()

# RAG V3 Clean pipeline (self-contained, zero external deps)
from assistant_rh_rag_pipeline import create_pipeline as create_pipeline_v3_clean
from assistant_rh_rag_pipeline.admin import get_rag_config, init_config_table, runtime_config_to_rag_config
from assistant_rh_rag_pipeline.chat_logger import build_log_row, build_non_rag_row
from assistant_rh_rag_pipeline.chat_logger import log_run as _log_run_v3
from assistant_rh_rag_pipeline.chat_logger import log_trace_events as _log_trace_events_v3
from assistant_rh_rag_pipeline.config import (
    DEFAULT_SYSTEM_PROMPT,
    get_prompt_content,
    today_fr,
)
from assistant_rh_rag_pipeline.db_helpers import create_engine_from_env, has_dsn
from assistant_rh_rag_pipeline.ministry_scope import MINISTRY_CATALOG
from assistant_rh_rag_pipeline.models import Chunk

from src.ui.admin_auth import is_admin
from src.ui.chatbot_feedback import (
    is_feedback_pending,
    render_feedback_block,
)

# Import UI utilities
from src.ui.chatbot_sources import (
    context_items_to_v1_chunks,
    extract_legal_refs_for_display,
    is_negative_response,
    render_sources,
    should_hide_sources,
)
from src.ui.cookies_security import is_production_like_env, resolve_cookies_password
from src.ui.groups import ADMIN_GROUP, DEFAULT_BADGE, valid_groups
from src.ui.user_groups_store import (
    get_group_policy,
    group_badge_display,
    group_priorities,
    known_group_slugs,
    resolve_group_retrieval_scope,
)

# --- Defaults dynamiques selon l'environnement ---
PG_AVAILABLE = bool(has_dsn() or os.getenv("PGHOST"))
ALBERT_OK = bool(os.getenv("ALBERT_API_KEY"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEALTH CHECK - Vérification des services au démarrage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@st.cache_data(ttl=300, show_spinner=False)  # Cache 5 min
def run_health_check() -> dict:
    """
    Vérifie l'état des services critiques au démarrage.
    Retourne un dict avec le statut de chaque service.
    """
    import requests

    health = {
        "db": {"status": "unknown", "message": ""},
        "albert_llm": {"status": "unknown", "message": ""},
        "albert_embed": {"status": "unknown", "message": ""},
        "scaleway": {"status": "unknown", "message": ""},
    }

    # 1. Database
    try:
        engine = create_engine_from_env()
        if engine:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            health["db"] = {"status": "ok", "message": "PostgreSQL connecté"}
        else:
            health["db"] = {"status": "warning", "message": "Pas de DB configurée (mode CSV)"}
    except Exception as e:
        health["db"] = {"status": "error", "message": f"DB error: {str(e)[:50]}"}

    # 2. Albert LLM
    try:
        albert_key = os.getenv("ALBERT_API_KEY")
        albert_url = os.getenv("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1")
        if albert_key:
            r = requests.get(f"{albert_url}/models", headers={"Authorization": f"Bearer {albert_key}"}, timeout=5)
            if r.status_code == 200:
                health["albert_llm"] = {"status": "ok", "message": "Albert API OK"}
            else:
                health["albert_llm"] = {"status": "error", "message": f"HTTP {r.status_code}"}
        else:
            health["albert_llm"] = {"status": "warning", "message": "ALBERT_API_KEY non configurée"}
    except Exception:
        health["albert_llm"] = {"status": "error", "message": "Albert timeout/error"}

    # 3. Albert Embeddings (same API, different check)
    health["albert_embed"] = health["albert_llm"].copy()  # Same API

    # 4. Scaleway
    try:
        scw_key = os.getenv("SCALEWAY_API_KEY")
        scw_url = os.getenv("SCALEWAY_BASE_URL", "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1")
        if scw_key:
            r = requests.get(f"{scw_url}/models", headers={"Authorization": f"Bearer {scw_key}"}, timeout=5)
            if r.status_code == 200:
                health["scaleway"] = {"status": "ok", "message": "Scaleway API OK"}
            else:
                health["scaleway"] = {"status": "error", "message": f"HTTP {r.status_code}"}
        else:
            health["scaleway"] = {"status": "warning", "message": "SCALEWAY_API_KEY non configurée"}
    except Exception:
        health["scaleway"] = {"status": "error", "message": "Scaleway timeout/error"}

    return health


def log_health_status():
    """Log le statut des services au démarrage (une seule fois par session)."""
    if "health_check_done" in st.session_state:
        return

    health = run_health_check()

    # Construire le log
    status_icons = {"ok": "✅", "warning": "⚠️", "error": "❌", "unknown": "❓"}
    parts = []
    for service, info in health.items():
        icon = status_icons.get(info["status"], "❓")
        parts.append(f"{service}:{icon}")

    print(f"🏥 Health: {' | '.join(parts)}")

    # Alerter si services critiques down
    critical_down = []
    if health["db"]["status"] == "error":
        critical_down.append("Database")
    if health["albert_llm"]["status"] == "error" and health["scaleway"]["status"] == "error":
        critical_down.append("Tous les LLMs")

    if critical_down:
        print(f"🚨 ALERTE: Services critiques indisponibles: {', '.join(critical_down)}")

    st.session_state.health_check_done = True
    st.session_state.health_status = health


# 🏥 Run health check at startup
log_health_status()

# --- PostgreSQL connection for logging ---
from src.ui.db_utils import get_engine


def safe_round(x, ndigits: int = 2):
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


# ------------------------------
# Page config & small CSS polish
# ------------------------------
# Try to set page config to wide layout with sidebar collapsed by default
# If already set (via Home.py), this will fail silently
try:
    st.set_page_config(page_title="Chatbot", page_icon="🪄", layout="wide", initial_sidebar_state="expanded")
except Exception:
    pass  # Config already set, ignore

# Animation: Auto-expand sidebar after page load (smooth UX)
st.markdown(
    """
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20,400,0,0" rel="stylesheet">
<style>
/* Page navigation visibility is decided below, once the user group is known
   (admins keep the full nav; everyone else has it hidden). */
/* Hide deprecation warning banners */
div[data-testid="stAlert"] .stAlert { display: none !important; }
div.stAlert:has(> div[role="alert"]) { display: none !important; }
:root {
    --blue-france: #003091;
    --violet-france: #696AF4;
    --red-marianne: #E10110;
    --green-emeraude: #18753C;
    --grey-950: #161616;
    --grey-200: #E5E5E5;
    --grey-50: #F6F6F6;
}
div.stChatMessage > div { padding: 0.6rem 0.8rem; }
details > summary { font-weight: 600; color: var(--grey-950); }
.st-emotion-cache-yd4u6l:focus-within {border-color: var(--blue-france);}
.st-emotion-cache-1yk2xem:focus-within {border-color: var(--blue-france);}
.chunk-card { border: 1px solid var(--grey-200); border-radius: 8px; padding: 0.6rem 0.8rem; margin-bottom: 0.5rem; background: var(--grey-50); }
.chunk-card-used { border: 2px solid #10b981; border-radius: 8px; padding: 0.6rem 0.8rem; margin-bottom: 0.5rem; background: #f0fdf4; box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.1); }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#eef2ff; color:#4f46e5; font-weight: 500; }
.muted { color: #666666; font-size: 12px; }
.stButton > button[kind="primary"] { background-color: var(--blue-france) !important; color: white !important; border: none !important; font-weight: 500 !important; transition: background-color 0.2s ease; }
.stButton > button[kind="primary"]:hover { background-color: #0041b3 !important; }
.stButton > button { border-radius: 4px !important; font-weight: 500 !important; }
.stTextInput > div > div > input:focus { border-color: var(--blue-france) !important; box-shadow: 0 0 0 1px var(--blue-france) !important; }
.dsfr-header { display: flex; align-items: center; gap: 16px; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 2px solid var(--grey-200); }
.dsfr-accent-bar { width: 4px; height: 48px; background: var(--blue-france); border-radius: 2px; }
.dsfr-title { margin: 0; color: var(--grey-950); font-size: 2rem; font-weight: 700; line-height: 1.2; }
.dsfr-subtitle { margin: 4px 0 0 0; color: #666666; font-size: 0.875rem; font-weight: 400; }
.dsfr-welcome-title { margin: 0 0 8px 0; font-weight: 600; color: var(--grey-950); font-size: 1rem; }
.dsfr-welcome-warning { margin: 8px 0; padding: 8px 12px; background: #FFF5E6; border-left: 3px solid #FF9940; border-radius: 4px; color: var(--grey-950); font-size: 0.875rem; }
.dsfr-welcome-text { margin: 8px 0 0 0; color: var(--grey-950); font-size: 0.9375rem; }
.material-symbols-outlined { font-family: 'Material Symbols Outlined'; font-weight: normal; font-style: normal; font-size: 24px; line-height: 1; letter-spacing: normal; text-transform: none; display: inline-block; white-space: nowrap; word-wrap: normal; direction: ltr; vertical-align: middle; }
/* Style pour les cartes de suggestions (inspiré de dsfr-welcome-warning) */
/* Cibler uniquement les boutons secondary (suggestions) */
button[kind="secondary"] .st-emotion-cache-12j140x {
    width: 100% !important;
    text-align: left !important;
    padding: 8px 12px !important;
    border-radius: 4px !important;
    border: none !important;
    border-left: 3px solid var(--blue-france) !important;
    background: #f0f2ff !important;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05) !important;
    transition: all 0.2s ease !important;
    min-height: 32px !important;
    height: auto !important;
    margin-bottom: 0px !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x p {
    font-size: 14px !important;
    margin: 0 !important;
    padding: 0 !important;
    white-space: normal !important;
    line-height: 1.3 !important;
    color: var(--grey-950) !important;
    font-weight: 400 !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x:hover {
    background: #e0e5ff !important;
    border-left-color: #0041b3 !important;
    box-shadow: 0 3px 6px rgba(0, 0, 145, 0.12) !important;
    cursor: pointer !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x:active {
    transform: translateX(1px) !important;
    box-shadow: 0 1px 3px rgba(0, 0, 145, 0.15) !important;
}
/* Cibler le bouton parent pour les suggestions */
button[kind="secondary"] {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
    margin-bottom: 0 !important;
}
button[kind="secondary"]:hover {
    background: rgba(151, 166, 195, 0.15) !important;
}
.st-emotion-cache-liupih { padding: 3rem 5rem 5rem; }
.st-emotion-cache-10p9htt {
    height: 3.5rem !important;
}
button[kind="secondary"] .st-emotion-cache-9114l4:hover { background-color: var(--grey-200) !important; }

/* Feedback V2: Le widget natif st.feedback gère le style automatiquement */

</style>
""",
    unsafe_allow_html=True,
)
# .dsfr-welcome { border-left: 4px solid var(--blue-france); padding-left: 16px; margin: 16px 0; }


# ------------------------------
# Data structures
# ------------------------------
@dataclass
class Turn:
    user: str
    assistant: str
    retrieved: List[Chunk]
    prompt_used: Optional[str] = None  # the actual LLM prompt with context
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    feedback: Optional[Dict[str, Any]] = None
    legal_refs: Optional[List] = None  # Références juridiques extraites (MatchedReference)


# ---------- Feedback storage ----------
# In production, use /tmp to avoid triggering file watcher
# In development, use local data/ directory for persistence
_IS_PRODUCTION = is_production_like_env()
if _IS_PRODUCTION:
    BASE = Path("/tmp/assistant_rh_data")
else:
    BASE = Path(__file__).resolve().parent.parent / "data"

RUNS_CSV = BASE / "chat_runs" / "chat_runs.csv"
FEEDS_CSV = BASE / "chat_feedbacks" / "chat_feedbacks.csv"
REVIEWS_CSV = BASE / "chat_reviews" / "reviewed.csv"

for p in [RUNS_CSV.parent, FEEDS_CSV.parent, REVIEWS_CSV.parent]:
    p.mkdir(parents=True, exist_ok=True)

RUNS_FIELDS = [
    "ts",
    "turn_id",
    "trace_id",
    "question",
    "answer",
    "provider",
    "model",
    "temperature",
    "backend",
    "table",
    "embed_col",
    "filters",
    "top_k",
    "use_reranker",
    "reranker_name",
    "rerank_top_k",
    "retrieved",
    "prompt",
    "system_prompt",
    "system_prompt_name",
    "use_query_rewriting",
    "rewritten_query",
    "use_hyde",
    "hyde_document",
    "use_query_expansion",
    "expanded_query",
    "acronyms_used",
    "query_for_retrieval",  # Query finale envoyée au retriever (après tous les traitements)
    "retrieval_mode",
    "hybrid_alpha",
    "sparse_method",
    "session_id",
    "conversation_id",
    "turn_index",  # Tracking: session, conversation, et index du tour
    "sources_used_count",
    "sources_used_indices",
    "sources_used_content",
    "fallbacks_used",
    "sources_raw_line",
    "retrieval_time_ms",
    "rerank_time_ms",
    "llm_time_ms",
    "total_time_ms",
    "ttft_ms",
    "tokens_per_second",  # Performance LLM: Time to First Token, Throughput
    "dist_before_rerank",
    "dist_after_rerank",  # Distribution des sources avant/après reranking
    "boost_weights",  # Poids de boosting par source (JSON)
    # Query Pipeline
    "use_intent_gating",
    "intent_result",
    "intent_confidence",
    "intent_model",
    "use_query_reformulation",
    "reformulated_query",
    "reformulation_model",
    "pipeline_latency_ms",
    "direct_response",
    # RAG V2 tracking
    "rag_version",
    "chunk_selection_mode",
    "cascade_source",
    "expanded_refs_count",
    # A/B Testing
    "user_group",
    # Ministry routing (issue #341)
    "selected_ministry",
    # ═══════════════════════════════════════════════════════════════════════════
    # V3 OBSERVABILITY COLUMNS (42 new columns for detailed V3 pipeline tracking)
    # ═══════════════════════════════════════════════════════════════════════════
    # Context structure
    "v3_context_mode",
    "v3_sections_count",
    "v3_context_items_count",
    "v3_context_tokens",
    "v3_doc_entire_count",
    # LLM Selector details
    "v3_selector_confidence",
    "v3_selector_selected_count",
    "v3_need_more_context",
    "v3_missing_topics",
    "v3_suggested_expansion",
    "v3_selector_fallback_used",
    "v3_selector_fallback_reason",
    # Escalation tracking
    "v3_escalation_tier",
    "v3_escalation_count",
    "v3_tier_a_used",
    "v3_tier_b_used",
    "v3_tier_c_used",
    "v3_tier_c_query",
    # DGAFP legal retrieval
    "v3_dgafp_level",
    "v3_dgafp_articles_count",
    "v3_dgafp_keywords",
    # Legal refs breakdown
    "v3_legal_refs_total",
    "v3_legal_refs_from_expansion",
    "v3_legal_refs_from_dgafp",
    "v3_legal_refs_details",
    # Retrieval details
    "v3_chunks_retrieved_count",
    "v3_embedding_model",
    "v3_search_mode",
    "v3_reranker_enabled",
    "v3_reranker_model",
    "v3_rerank_top_k",
    # Timing breakdown
    "v3_query_processing_ms",
    "v3_retrieval_ms",
    "v3_aggregation_ms",
    "v3_context_building_ms",
    "v3_selector_ms",
    "v3_legal_refs_ms",
    "v3_dgafp_ms",
    "v3_generation_ms",
    "v3_timing_breakdown",
    # Query processing
    "v3_intent",
    "v3_intent_gating_enabled",
    "v3_should_proceed",
    "v3_acronyms_expanded",
    # Context items summary
    "v3_context_items_summary",
    "v3_source_distribution",
    # ═══════════════════════════════════════════════════════════════════════════
    # 🆕 V3 TIMING & METRICS COLUMNS (new for detailed observability)
    # ═══════════════════════════════════════════════════════════════════════════
    # Streaming metrics
    "v3_ttft_ms",
    "v3_chars_per_second",
    "v3_response_length",
    # Quality metrics
    "v3_avg_chunk_score",
    "v3_avg_section_score",
    "v3_top1_score",
    # Query enrichment
    "v3_was_enriched",
    "v3_enriched_query",
    # Raw data for debugging
    "v3_chunks_raw",
    "v3_sections_raw",
    "v3_context_before_selector",
    # Retrieval details (Module 2)
    "v3_retrieval_params",
    "v3_chunks_before_rerank",
    "v3_chunks_after_rerank",
    # Aggregation details (Module 3)
    "v3_aggregation_params",
    "v3_sections_before_rerank",
    "v3_sections_after_rerank",
]
FEEDS_FIELDS = [
    "ts",
    "turn_id",
    "turn_idx",
    "helpful",
    "reasons",
    "comment",
    "stars",
    "reasons_positive",
    "reasons_negative",
    "session_id",
    "question",
    "answer",
]
REVIEWS_FIELDS = ["turn_id", "reviewed", "question", "answer", "notes", "ts"]


def _append_csv_row(path: Path, fieldnames: list[str], row: dict):
    """Fallback CSV logging if PostgreSQL is unavailable."""
    from assistant_rh_rag_pipeline.chat_logger import _append_csv_row as _csv_row

    _csv_row(path, fieldnames, row)


def log_run_row(row: dict):
    """Log a chat run to PostgreSQL (or CSV fallback).

    Delegates to ``chat_logger.log_run`` for the dynamic UPSERT.
    """
    _log_run_v3(row, engine=get_engine(), csv_path=RUNS_CSV, csv_fields=RUNS_FIELDS)


def log_trace_event_rows(events: list[dict], *, turn_id: str, trace_id: str):
    """Persist detailed RAG trace events and optionally export OTEL spans."""
    _log_trace_events_v3(events, turn_id=turn_id, trace_id=trace_id, engine=get_engine())


def upsert_reviews(turn_ids: list[str], reviewed: bool):
    """Update/insert reviews in PostgreSQL (or CSV fallback)."""
    engine = get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                for tid in turn_ids:
                    conn.execute(
                        text("""
                        INSERT INTO chat_reviews (turn_id, reviewed, ts, updated_at)
                        VALUES (:turn_id, :reviewed, NOW(), NOW())
                        ON CONFLICT (turn_id) DO UPDATE SET
                            reviewed = EXCLUDED.reviewed,
                            updated_at = NOW()
                    """),
                        {"turn_id": tid, "reviewed": reviewed},
                    )
                conn.commit()
            return
        except Exception as e:
            st.warning(f"PostgreSQL review update failed, using CSV fallback: {e}")

    # Fallback to CSV
    lock = FileLock(str(REVIEWS_CSV) + ".lock")
    with lock:
        # charge existant -> dict pour reviewed, question, answer et notes
        existing = {}
        existing_notes = {}
        existing_questions = {}
        existing_answers = {}
        if REVIEWS_CSV.exists():
            with REVIEWS_CSV.open("r", newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    existing[r["turn_id"]] = r["reviewed"] == "True"
                    # Charger les notes si elles existent
                    existing_notes[r["turn_id"]] = r.get("notes", "")
                    # Charger la question si elle existe
                    existing_questions[r["turn_id"]] = r.get("question", "")
                    # Charger la réponse si elle existe
                    existing_answers[r["turn_id"]] = r.get("answer", "")
        # maj
        for tid in turn_ids:
            existing[tid] = reviewed
        # réécrit complet
        with REVIEWS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=REVIEWS_FIELDS)
            w.writeheader()
            now = dt.datetime.now(dt.UTC).isoformat()
            for tid, val in existing.items():
                w.writerow(
                    {
                        "turn_id": tid,
                        "reviewed": bool(val),
                        "question": existing_questions.get(tid, ""),
                        "answer": existing_answers.get(tid, ""),
                        "notes": existing_notes.get(tid, ""),
                        "ts": now,
                    }
                )


def _serialize_retrieved(chunks: List[Chunk]) -> str:
    """Sérialise les chunks pour stockage CSV (tolère différents schémas)."""
    mini = [
        {
            "id": c.id,
            "score": round(c.score, 4),
            "source": c.metadata.get("source"),
            "source_name": c.metadata.get("source_name"),
            "doc_title": c.metadata.get("doc_title"),
            "page": c.metadata.get("page"),
            "url": c.metadata.get("url"),
        }
        for c in chunks
    ]
    return json.dumps(mini, ensure_ascii=False)


# --- Load runtime configuration from DB ---
if "rag_config_initialized" not in st.session_state:
    init_config_table()
    st.session_state.rag_config_initialized = True

# Initialize cookies early so user group is available before cached helpers run.
cookies = EncryptedCookieManager(prefix="assistant_rh_", password=resolve_cookies_password())
if not cookies.ready():
    st.stop()

# Logout: a previous run cleared the group cookie and asked to return to the
# group picker. Do it now (the cookie write has flushed on this rerun).
if st.session_state.pop("_pending_logout", False):
    st.switch_page("Home.py")

_cookies_to_save = {}


@st.cache_data(ttl=30, show_spinner=False)
def _group_resolution_maps() -> tuple[dict[str, int], set[str]]:
    """(priority-by-slug, known-slugs) from the store — DB-authoritative, seed fallback."""
    return group_priorities(), known_group_slugs()


# Priorities and the known-set come from the DB store so admin-created groups
# carry their real priority (not 0) and are recognised. URL onboarding stays
# restricted to seed groups (VALID_GROUPS): new groups are password-only.
GROUP_PRIORITY, KNOWN_GROUPS = _group_resolution_maps()
VALID_GROUPS = valid_groups()


def _determine_user_group() -> tuple[str, bool]:
    """
    Determine user group with hierarchical protection.
    Returns (group, needs_save) - does NOT save cookies.
    """
    query_params = st.query_params
    reset_requested = query_params.get("reset_group") == "true"

    existing_group = cookies.get("user_group")
    # Ignore a cookie pointing at a group that no longer exists (deleted), so a
    # stale cookie can't keep authenticating; treat it as unassigned.
    if existing_group and existing_group not in KNOWN_GROUPS:
        existing_group = None
    existing_priority = GROUP_PRIORITY.get(existing_group, 0)

    url_group = query_params.get("group", "").lower()
    if url_group and url_group not in VALID_GROUPS:
        url_group = ""
    if url_group == ADMIN_GROUP:
        url_group = ""
    url_priority = GROUP_PRIORITY.get(url_group, 0)

    if reset_requested and url_group:
        return url_group, True
    elif existing_group and url_group:
        if url_priority > existing_priority:
            return url_group, True
        else:
            return existing_group, False
    elif existing_group:
        return existing_group, False
    elif url_group:
        return url_group, True
    else:
        return "default", True


user_group, group_needs_save = _determine_user_group()
if group_needs_save:
    _cookies_to_save["user_group"] = user_group
st.session_state.user_group = user_group

# Enforce the group picker: a visitor who reaches the chatbot without an
# identified group ("default") is sent back to the homepage to pick one. Admins
# (authenticated via the password form, no group cookie) are exempt.
if user_group == "default" and not is_admin():
    st.switch_page("Home.py")

# Page navigation visibility: admins keep the full sidebar page list; every
# other group has it hidden so only the chatbot is reachable from the nav.
if not is_admin():
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] { display: none !important; }
        nav[data-testid="stSidebarNav"] { display: none !important; }
        [data-testid="stSidebarNavItems"] { display: none !important; }
        ul[data-testid="stSidebarNavItems"] { display: none !important; }
        [data-testid="stSidebar"] nav { display: none !important; }
        [data-testid="stSidebar"] [data-testid="stSidebarNavSeparator"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_runtime_config():
    return get_rag_config()


rag_config = load_runtime_config()


def _ministry_label(ministry_id: str) -> str:
    ministry = MINISTRY_CATALOG.get(ministry_id)
    return ministry.label if ministry else ministry_id


# ------------------------------
# Sidebar avec filtres utilisateur (VERSION PRODUCTION - épurée)
# ------------------------------
with st.sidebar:
    # Group indicator + logout — shown for any identified user (admins always);
    # colours/labels come from the DB store with seed fallback.
    user_group = st.session_state.get("user_group", "default")
    if is_admin() or user_group != "default":
        icon, color, label = group_badge_display().get(user_group, (*DEFAULT_BADGE, user_group))

        st.markdown(
            f"""
        <div style="
            background: linear-gradient(135deg, {color}22, {color}11);
            border-left: 3px solid {color};
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 8px;
            font-size: 0.85em;
        ">
            {icon} <strong>Groupe:</strong> {label}
            <br><span style="color: #888; font-size: 0.8em;">Session: {st.session_state.get("session_id", "N/A")}</span>
        </div>
        """,
            unsafe_allow_html=True,
        )

        if st.button(
            "✕ Déconnexion",
            key="logout_btn",
            use_container_width=True,
            help="Se déconnecter et revenir à la sélection du groupe",
        ):
            # Reset to the "default" (unassigned) sentinel instead of deleting:
            # EncryptedCookieManager.__delitem__ is a no-op when a cookie prefix
            # is set, so pop/del would not actually clear the group. "default" is
            # treated as logged-out everywhere (picker shown, is_admin false).
            cookies["user_group"] = "default"
            cookies.save()
            # Also drop the active chat so it can't leak to the next group/user.
            for _k in (
                "user_group",
                "admin_authenticated",
                "_is_admin_cache",
                "turns",
                "conversation_id",
                "selected_ministry",
                "selected_ministry_picker",
            ):
                st.session_state.pop(_k, None)
            st.session_state["_pending_logout"] = True
            st.rerun()

    st.markdown("### 🗂️ Filtres")

    retrieval_scope = None
    retrieval_scope_error = ""
    selected_ministere = ""
    group_policy = get_group_policy(user_group)
    if not group_policy["valid"]:
        retrieval_scope_error = f"Configuration ministérielle invalide pour ce groupe : {group_policy['error']}"
        st.error(retrieval_scope_error)
    else:
        ministere_options = list(group_policy["allowed_ministries"])
        default_ministry = group_policy["default_ministry"]
        previous_ministry = st.session_state.get("selected_ministry")
        initial_ministry = previous_ministry if previous_ministry in ministere_options else default_ministry
        selected_ministere = st.selectbox(
            "Ministère",
            ministere_options,
            index=ministere_options.index(initial_ministry),
            format_func=_ministry_label,
            help="Le ministère sélectionné détermine les sources ministérielles interrogées pour cette requête.",
            key="selected_ministry_picker",
        )
        if previous_ministry and previous_ministry != selected_ministere:
            st.session_state.turns = []
            st.session_state.conversation_id = str(uuid.uuid4())[:8]
            st.session_state.selected_ministry = selected_ministere
            st.rerun()
        st.session_state.selected_ministry = selected_ministere
        retrieval_scope, retrieval_scope_error = resolve_group_retrieval_scope(user_group, selected_ministere)
        if retrieval_scope_error:
            st.error(retrieval_scope_error)

    # Filtre Agent (disabled - single option for now)
    agent_options = ["Contractuel"]
    selected_agent = st.selectbox(
        "Agent",
        agent_options,
        index=0,
        disabled=True,
        help="D'autres populations seront disponibles prochainement",
    )

# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION DEFAULTS – RAG V3 Clean parameters from Admin Config
# ═══════════════════════════════════════════════════════════════════════════════

# Paramètres V3 depuis Admin Config (avec fallback aux valeurs V2)
v3_context_mode = getattr(rag_config, "v3_context_mode", "standard")  # narrow/standard/wide
v3_search_mode = getattr(rag_config, "v3_search_mode", "semantic")  # semantic/hybrid/lexical
v3_token_budget = getattr(rag_config, "v3_token_budget", 8000)
v3_enable_escalation = getattr(rag_config, "v3_enable_escalation", True)
v3_enable_selector = getattr(rag_config, "v3_enable_selector", True)
v3_selector_model = getattr(rag_config, "v3_selector_model", "openweight-large")  # 🔄 Depuis Admin Config
v3_generator_model = getattr(rag_config, "v3_generator_model", "openweight-large")  # 🔄 Depuis Admin Config
v3_temperature = getattr(rag_config, "v3_temperature", 0.0)  # 🔄 Depuis Admin Config
v3_system_prompt_name = getattr(rag_config, "v3_system_prompt_name", "system_prompt_V6_optimized.md")  # 🔄 Depuis Admin Config

# ═══════════════════════════════════════════════════════════════════════════
# V3 RETRIEVAL PARAMETERS (paramètres dédiés, indépendants de V2)
# ═══════════════════════════════════════════════════════════════════════════
v3_initial_top_k = getattr(rag_config, "v3_initial_top_k", 10)  # 🔄 Depuis Admin Config
v3_enable_reranker = getattr(rag_config, "v3_enable_reranker", True)  # 🔄 Depuis Admin Config
v3_rerank_top_k = getattr(rag_config, "v3_rerank_top_k", 5)  # 🔄 Depuis Admin Config
v3_alpha = getattr(rag_config, "v3_alpha", 0.5)  # 🔄 Depuis Admin Config (pour mode hybride)

# LLM Configuration (valeurs fixes depuis config admin)
llm_config = {
    "provider": rag_config.llm_provider,
    "model": rag_config.llm_model,
    "temperature": rag_config.temperature,
}

# System prompt - Chargé depuis la config admin (ou fallback sur défaut)
_prompt_name = rag_config.system_prompt_name
_prompt_content = get_prompt_content(_prompt_name) if _prompt_name else None
if _prompt_content:
    # Remplacer {today} par la date actuelle
    system_prompt = _prompt_content.replace("{today}", today_fr())
else:
    system_prompt = DEFAULT_SYSTEM_PROMPT

# Log V3 config (once per session)
if "config_logged" not in st.session_state:
    st.session_state.config_logged = True
    print(
        f"[V3 Clean] generator={v3_generator_model}, prompt={v3_system_prompt_name}, "
        f"top_k={v3_initial_top_k}, selector={'ON' if v3_enable_selector else 'OFF'}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# App state
# ═══════════════════════════════════════════════════════════════════════════════
if "turns" not in st.session_state:
    st.session_state.turns = []

# ═══════════════════════════════════════════════════════════════════════════════
# Cookie Manager (session tracking, user group, disclaimer)
# ═══════════════════════════════════════════════════════════════════════════════
if "session_id" not in st.session_state:
    existing_session_id = cookies.get("session_id")
    if existing_session_id:
        st.session_state.session_id = existing_session_id
    else:
        new_session_id = str(uuid.uuid4())[:8]
        st.session_state.session_id = new_session_id
        _cookies_to_save["session_id"] = new_session_id

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = str(uuid.uuid4())[:8]


# 3. SINGLE SAVE for all cookies
if _cookies_to_save:
    for key, value in _cookies_to_save.items():
        cookies[key] = value
    cookies.save()  # ← Une seule fois !

# ------------------------------
# Loading indicator for first visit
# ------------------------------
# DISABLED: This block causes crashes during Streamlit's page indexing phase
# because retriever and llm may not be defined yet. The models will load
# lazily when the user first interacts with the app.
# if "page_ready" not in st.session_state:
#     st.session_state.page_ready = True


# ------------------------------
# ⚠️ Disclaimer Modal (first visit only)
# ------------------------------
@st.dialog("Assistant RH - Beta test", width="small")
def show_disclaimer_modal():
    """Affiche la modale d'avertissement pour les nouveaux utilisateurs."""
    # CSS pour enlever le border rouge du bouton close
    st.markdown(
        """
    <style>
        div[data-testid="stDialog"] button[aria-label="Close"] {
            outline: none !important;
            border: none !important;
            box-shadow: none !important;
        }
        div[data-testid="stDialog"] button[aria-label="Close"]:focus {
            outline: none !important;
            border: none !important;
            box-shadow: none !important;
        }
    </style>
    """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
    <div style="text-align: center; padding: 20px 8px;">
        <p style="font-size: 1.05em; line-height: 1.6; color: #333; margin: 0;">
            ⚠️ Les réponses générées par l'IA doivent être <strong>relues, complétées et validées</strong> 
            avant toute utilisation et diffusion auprès des agents.
        </p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # Espace supplémentaire avant le bouton
    st.write("")

    # Bouton centré
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("J'ai compris", type="primary", width="stretch"):
            # Marquer comme accepté dans le cookie (persiste entre sessions)
            cookies["disclaimer_accepted"] = "true"
            cookies.save()
            st.session_state.disclaimer_accepted = True
            st.rerun()


# Vérifier si l'utilisateur a déjà accepté le disclaimer
disclaimer_accepted = cookies.get("disclaimer_accepted") == "true"

# Afficher la modale si pas encore accepté
if not disclaimer_accepted and not st.session_state.get("disclaimer_accepted", False):
    show_disclaimer_modal()

# ========== DSFR Header ==========
col1, col2 = st.columns([8, 2])
with col1:
    st.markdown(
        """
    <div class="dsfr-header">
        <div class="dsfr-accent-bar"></div>
        <div>
            <h1 class="dsfr-title">Assistant RH</h1>
            <p class="dsfr-subtitle">Spécialisé contractuels - Fonction Publique d'État</p>
        </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # Précharger la connexion DB en arrière-plan (silencieux)
    _ = get_engine()

    with st.chat_message("assistant"):
        st.markdown(
            """
        <div class="dsfr-welcome">
            <p class="dsfr-welcome-title">
                Bonjour, je suis l'<strong>Assistant RH</strong> spécialisé sur les questions liées aux <strong>contractuels (FPE)</strong> !
            </p>
            <div class="dsfr-welcome-warning">
                <strong>⚠️ Prototype en cours de développement</strong><br>
                Vos retours sont précieux et m'aideront à améliorer mes performances !
            </div>
            <p class="dsfr-welcome-text">
                Comment puis-je vous aider aujourd'hui ?
            </p>
        </div>
        """,
            unsafe_allow_html=True,
        )

# ═══════════════════════════════════════════════════════════════════════════════
# SUGGESTIONS HARDCODÉES
# Questions choisies manuellement pour guider les utilisateurs
# 3 questions tirées au hasard à chaque "New chat" ou rechargement
# ═══════════════════════════════════════════════════════════════════════════════

SUGGESTIONS_POOL = [
    # Recrutement
    "Comment recruter un agent contractuel ?",
    "Quels sont les motifs de recrutement d'un contractuel ?",
    "Quelle est la durée maximale d'un CDD ?",
    # Contrat
    "Quand renouveler le contrat d'un contractuel ?",
    "Quel est le délai minimal pour informer un agent de la fin de son contrat ?",
    "Comment passer d'un CDD à un CDI ?",
    # Rémunération
    "Comment est fixée la rémunération d'un agent contractuel ?",
    "À quelle fréquence la rémunération doit-elle être réévaluée ?",
    "Un contractuel a-t-il droit à des primes ?",
    # Congés et temps de travail
    "Les contractuels ont-ils droit aux mêmes congés que les fonctionnaires ?",
    "Un agent contractuel peut-il demander un temps partiel ?",
    "Qu'est-ce que le congé de mobilité ?",
    # Fin de contrat
    "Comment licencier un agent contractuel ?",
    "Un contractuel a-t-il droit à une indemnité de licenciement ?",
    "Quelles sont les conditions de démission d'un contractuel ?",
]

# Sélectionner 3 suggestions aléatoires (stockées en session pour stabilité)
if "suggestions" not in st.session_state:
    st.session_state.suggestions = random.sample(SUGGESTIONS_POOL, 3)

with col2:
    st.write("")
    st.write("")
    st.write("")
    new_chat = st.button(label="**:material/refresh: New chat**", key="new", width="stretch", type="primary")
    if new_chat:
        st.session_state.turns = []
        # Générer un nouveau conversation_id pour le nouveau fil de discussion
        st.session_state.conversation_id = str(uuid.uuid4())[:8]
        # 🔄 Régénérer 3 nouvelles suggestions aléatoires
        st.session_state.suggestions = random.sample(SUGGESTIONS_POOL, 3)
        st.rerun()

    # Espacement pour aligner "Suggestions" avec le message d'accueil
    st.markdown('<div style="margin-top: 20px;"></div>', unsafe_allow_html=True)

    st.write("")
    st.markdown('<p style="margin-bottom: 8px; font-weight: 600; font-size: 14px;">💡 Suggestions</p>', unsafe_allow_html=True)

    # Bloquer les suggestions si un feedback est en attente
    feedback_pending = is_feedback_pending()
    rag_scope_blocked = bool(retrieval_scope_error or retrieval_scope is None)

    # Afficher les 3 suggestions aléatoires (tirées au sort à chaque nouveau chat)
    for i, suggestion in enumerate(st.session_state.suggestions):
        if st.button(suggestion, key=f"sug_{i}", width="stretch", type="secondary", disabled=feedback_pending or rag_scope_blocked):
            st.session_state.suggested_query = suggestion
            st.rerun()


def _detect_source_type(chunk: Chunk) -> str:
    """Détecte la source du chunk (MATTE, Service Public, DGAFP, RGRH, RAG3)."""
    meta = chunk.metadata or {}
    table_source = meta.get("table_source", "")

    # Si table_source est explicite (multi-source retriever)
    if table_source:
        # Mapper les noms de tables vers les noms lisibles
        table_to_name = {
            "rag_chunks_matte": "MATTE",
            "rag_chunks_mso": "MSO",
            "rag_chunks_fiches_sp": "Service Public",
            "rag_chunks_dgafp": "DGAFP",
            "rag_chunks_3": "RAG3",  # Nouvelle source
            "rag_chunks_rgrh": "RGRH",  # Nouvelle source
        }
        return table_to_name.get(table_source, table_source)

    # Sinon, déduire depuis les métadonnées
    publisher = str(meta.get("source") or meta.get("publisher") or "").lower()
    if publisher == "mso":
        return "MSO"
    if meta.get("cid") or meta.get("nature"):
        return "DGAFP"
    elif meta.get("sid") or meta.get("audience") or meta.get("theme"):
        return "Service Public"
    elif meta.get("qa_id") or meta.get("role") or meta.get("section_path"):
        # Distinguer RGRH/RAG3 de MATTE par les métadonnées
        return "MATTE"
    else:
        return "Inconnu"


def _source_badge_html(source: str) -> str:
    """Retourne un badge HTML coloré selon la source (couleurs DSFR officielles)."""
    colors = {
        "MATTE": "#696AF4",  # Violet France (DSFR)
        "MSO": "#7c3aed",
        "Service Public": "#3b82f6",  # Bleu France (DSFR)
        "DGAFP": "#18753C",  # Vert émeraude (DSFR)
        "RGRH": "#F95C5E",  # Rouge Marianne (DSFR) - nouvelles fiches
        "RAG3": "#E18B76",  # Orange terre battue (DSFR) - rag_chunks_3
        "Inconnu": "#666666",  # Gris
    }
    color = colors.get(source, "#6B7280")
    return f"<span style='display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; color:white; background:{color}; margin-right:6px;'>{source}</span>"
    # color = colors.get(source, "#666666")
    # return f"<span style='display:inline-block; padding:4px 12px; border-radius:4px; font-size:12px; font-weight:500; color:white; background:{color}; margin-right:8px;'>{source}</span>"


def render_debug_chunks(
    title: str,
    chunks: List[Chunk],
    model_name: str,
    ctx: dict | None = None,
    unique_key: str = "default",
    used_indices: List[int] | None = None,
    prompt: str | None = None,
) -> None:
    # ctx: infos utiles (backend, table, embed_col, filtres) à afficher quand 0 chunk
    with st.expander(f"🔍 {title} – LLM: {model_name}", expanded=False):
        # 🔤 Query Processing info (si disponible dans ctx)
        if ctx:
            query_info = ctx.get("query_info")
            acronyms = ctx.get("acronyms")
            if query_info or acronyms:
                st.markdown("##### 🔤 Query Processing")
                if query_info:
                    st.code(query_info, language=None)
                if acronyms:
                    st.info(f"📝 Acronymes expansés: {acronyms}")
                st.markdown("---")

        if not chunks:
            st.warning("Aucun chunk récupéré.")
            if ctx:
                st.caption(
                    f"Backend: **{ctx.get('backend')}** · Table: **{ctx.get('table')}** · "
                    f"Embed: **{ctx.get('embed_col')}** · Filtres: "
                    f"{', '.join(f'{k}={v}' for k, v in (ctx.get('filters') or {}).items() if v) or '—'}"
                )
                st.caption("Tips: vérifie `ALBERT_API_KEY`, le DSN (`SCW_POSTGRES_DSN`), les filtres et que la colonne d'embedding n'est pas vide.")
            return

        # Compter les chunks par source
        from collections import Counter

        source_counts = Counter(_detect_source_type(c) for c in chunks)

        # Afficher le résumé avec badges colorés
        source_summary = " · ".join([f"{_source_badge_html(src)} {count}" for src, count in source_counts.most_common()])
        st.markdown(
            f"<div style='margin-bottom:12px;'>"
            f"<span style='color:#6B7280; font-size:14px;'>Total: {len(chunks)} chunks</span><br>"
            f"{source_summary}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # tableau compact avec colonne Source
        rows = []
        for i, c in enumerate(chunks):
            source_type = _detect_source_type(c)
            meta = c.metadata or {}

            # Titre harmonisé selon la source
            if source_type == "DGAFP":
                titre = meta.get("title") or meta.get("full_title") or "-"
                detail = meta.get("number") or ""  # Numéro d'article pour DGAFP
            elif source_type == "Service Public":
                titre = meta.get("title") or "-"
                detail = meta.get("number") or ""  # Numéro de fiche pour Service Public
            else:  # MATTE
                titre = meta.get("source_name") or "-"
                detail = ""  # Vide pour MATTE

            # Indicateur si chunk utilisé
            is_used = used_indices and (i + 1) in used_indices
            chunk_indicator = f"✅ {i + 1}" if is_used else str(i + 1)

            rows.append(
                {
                    "#": chunk_indicator,
                    "Source": source_type,
                    "Titre": titre,
                    "Détail": detail,
                    "Δrank": meta.get("orig_rank", i + 1) - (i + 1),
                    "retrieval": safe_round(meta.get("retrieval_score")),
                    "rerank": safe_round(meta.get("rerank_score")),
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

        # 📝 Afficher le prompt complet si fourni (utile pour debug)
        if prompt:
            with st.expander("📄 Prompt complet envoyé au LLM", expanded=False):
                st.code(prompt, language="markdown", line_numbers=False)

        payload = [asdict(c) for c in chunks]
        # st.download_button(
        #     "Download retrieval JSON for this turn",
        #     data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        #     file_name="retrieved_chunks.json",
        #     mime="application/json",
        #     width="stretch",
        #     key=f"download_{unique_key}",  # ← UNIQUE KEY
        # )

        st.markdown("<div class='muted'>Full chunk texts</div>", unsafe_allow_html=True)
        for i, c in enumerate(chunks, start=1):
            meta = c.metadata or {}
            source_type = _detect_source_type(c)
            badge_html = _source_badge_html(source_type)

            # URL (si disponible)
            url = meta.get("url")
            if not url and meta.get("cid"):
                # Générer URL Légifrance pour DGAFP
                url = f"https://www.legifrance.gouv.fr/codes/id/{meta.get('cid')}/"
            link_html = f" · <a href='{url}' target='_blank'>ouvrir</a>" if url else ""

            # Scores
            retr_val = safe_round(meta.get("retrieval_score"))
            rer_val = safe_round(meta.get("rerank_score"))
            retr_str = f"{retr_val:.2f}" if isinstance(retr_val, float) else "-"
            rer_str = f"{rer_val:.2f}" if isinstance(rer_val, float) else "-"

            # Titre/identifiant selon la source
            if source_type == "DGAFP":
                title_str = meta.get("full_title") or meta.get("title") or meta.get("cid") or "-"
            elif source_type == "Service Public":
                title_str = meta.get("title") or meta.get("sid") or "-"
            else:  # MATTE
                title_str = meta.get("source_name") or meta.get("qa_id") or "-"

            # Style spécial si chunk utilisé
            is_used = used_indices and i in used_indices
            used_indicator = " ✅" if is_used else ""

            # Header avec badge, scores et titre (reste en HTML)
            st.markdown(
                f"{badge_html}"
                f"<span class='badge'>Chunk {i}{used_indicator}</span> "
                f"<span class='muted'>retr {retr_str} · rer {rer_str}</span><br>"
                f"<strong>{title_str}</strong>{link_html}",
                unsafe_allow_html=True,
            )

            # Contenu du chunk en raw text, taille fixe, scrollable
            st.text_area(
                label=f"Chunk {i} text",
                value=c.text or "(no text)",
                height=250,
                disabled=True,
                key=f"chunk_text_{unique_key}_{i}",
                label_visibility="collapsed",
            )


# Render history
for idx, t in enumerate(st.session_state.turns):
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(t.user)
    with st.chat_message("assistant"):
        # unsafe_allow_html=True pour supporter les <br/> dans les tableaux (GPT-OSS)
        st.markdown(t.assistant, unsafe_allow_html=True)
        # 📚 Afficher les sources pour l'historique (sauf si réponse négative ou sources à cacher)
        if not is_negative_response(t.assistant) and not should_hide_sources(t.assistant):
            render_sources(t.retrieved, key_suffix=f"history_{idx}", legal_refs=t.legal_refs)
        if rag_config.verbose_mode:
            with st.expander("Détails RAG", expanded=False):
                st.json([{"id": c.id, "score": round(c.score, 3), "source": c.metadata.get("source_name", "")} for c in (t.retrieved or [])])

        # Feedback seulement si réponse positive
        if not is_negative_response(t.assistant):
            render_feedback_block(t)
        # render_feedback_block(i, t, llm_config)


# # 🚨 Vérifier si un feedback est en attente avant de permettre une nouvelle question
# feedback_pending = _is_feedback_pending()
# if feedback_pending:
#     st.warning("⚠️ Veuillez compléter votre feedback (cochez au moins une raison et cliquez sur 'Envoyer') avant de poser une nouvelle question.")

rag_scope_blocked = bool(retrieval_scope_error or retrieval_scope is None)
query = st.chat_input("Posez votre question à notre assistant...", disabled=feedback_pending or rag_scope_blocked)

# Vérifier si une suggestion a été cliquée
if hasattr(st.session_state, "suggested_query") and st.session_state.suggested_query:
    query = st.session_state.suggested_query
    st.session_state.suggested_query = None  # Réinitialiser

if query:
    if retrieval_scope is None:
        st.error(retrieval_scope_error or "Configuration ministérielle indisponible.")
        st.stop()

    turn_id = str(uuid.uuid4())[:8]
    trace_id = uuid.uuid4().hex

    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(query)

    # ═══════════════════════════════════════════════════════════════════════════
    # RAG V3 CLEAN PIPELINE
    # ═══════════════════════════════════════════════════════════════════════════
    if True:
        try:
            # Créer la config V3 Clean
            config_v3 = runtime_config_to_rag_config(rag_config)

            pipeline_v3 = create_pipeline_v3_clean(config_v3)

            # 📜 Construire l'historique de conversation pour intent gating + query enrichment
            MAX_HISTORY_TURNS_V3 = 5
            conversation_history_v3 = []
            if st.session_state.turns:
                recent_turns = st.session_state.turns[-MAX_HISTORY_TURNS_V3:]
                for turn in recent_turns:
                    conversation_history_v3.append(
                        {
                            "role": "user",
                            "content": turn.user,
                        }
                    )
                    conversation_history_v3.append(
                        {
                            "role": "assistant",
                            "content": turn.assistant,
                        }
                    )

            # Exécuter le pipeline V3 Clean avec STREAMING
            t_v3_start = time.time()

            # Étape 1 : traitement de la requête (intent + acronymes)
            qr_v3 = pipeline_v3.process_query(query, conversation_history_v3, retrieval_scope=retrieval_scope)

            # Si hors-scope (chit-chat, etc.), répondre directement
            if not qr_v3.should_proceed:
                with st.chat_message("assistant"):
                    v3_response = qr_v3.direct_response or ""
                    st.markdown(v3_response)
                turn_obj = Turn(id=turn_id, user=query, assistant=v3_response, retrieved=[], prompt_used="")
                st.session_state.turns.append(turn_obj)

                # Log non-RAG turns for full conversation traceability
                try:
                    row = build_non_rag_row(
                        turn_id,
                        query,
                        v3_response,
                        qr_v3,
                        pipeline_v3,
                        dict(st.session_state),
                        runtime_config=rag_config,
                        trace_id=trace_id,
                        retrieval_scope=retrieval_scope,
                    )
                    log_run_row(row)
                except Exception as e:
                    import traceback

                    st.toast(f"⚠️ Log DB échoué (intent): {type(e).__name__}", icon="⚠️")
                    print(f"⚠️ LOG INTENT ERROR: {e}\n{traceback.format_exc()}")

                render_feedback_block(turn_obj)
                st.stop()

            with st.chat_message("assistant"):
                status_placeholder = st.empty()
                status_placeholder.caption("🔍 Analyse de la question...")

                def _update_status(msg: str):
                    status_placeholder.caption(f"⏳ {msg}")

                stream_generator = pipeline_v3.run_stream(
                    qr_v3,
                    conversation_history_v3,
                    on_status=_update_status,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    retrieval_scope=retrieval_scope,
                )

                def _stream_clear_on_first(gen, loader):
                    """Keep loader visible during retrieval, clear on first token."""
                    cleared = False
                    for chunk in gen:
                        if not cleared:
                            loader.empty()
                            cleared = True
                        yield chunk
                    if not cleared:
                        loader.empty()

                response_placeholder = st.empty()
                with response_placeholder:
                    v3_response_raw = st.write_stream(_stream_clear_on_first(stream_generator, status_placeholder))

                # 🧹 Handle HTML in response (GPT-OSS uses <br> for line breaks in tables)
                v3_response = v3_response_raw
                if "<br" in v3_response_raw:
                    # Normalize <br> variants to <br/>
                    v3_response = v3_response_raw.replace("<br>", "<br/>").replace("<br />", "<br/>")
                    # Re-render with HTML support for proper table formatting
                    response_placeholder.markdown(v3_response, unsafe_allow_html=True)

                t_v3_end = time.time()

                # ═══════════════════════════════════════════════════════════════
                # 📊 Récupérer les métadonnées V3 Clean
                # ═══════════════════════════════════════════════════════════════
                v3_result = pipeline_v3.last_result
                v3_metadata = v3_result.metadata if v3_result else {}
                v3_timing = v3_result.timing if v3_result else {}
                total_time_ms = (t_v3_end - t_v3_start) * 1000
                context_items = v3_result.context_items if v3_result else []
                total_tokens = sum(item.token_estimate for item in context_items)

                # Count individual legal ref entries across all context items
                refs_in_context = 0
                for it in context_items:
                    refs = it.references_juridiques
                    if not refs:
                        continue
                    if isinstance(refs, str):
                        try:
                            refs = json.loads(refs)
                        except Exception:
                            continue
                    if isinstance(refs, list):
                        refs_in_context += len(refs)
                    elif isinstance(refs, dict):
                        refs_in_context += 1

                # Count resolved refs (actually injected from DGAFP into context)
                refs_injected = len(getattr(pipeline_v3._context_builder, "last_resolved_refs", {}) or {})

                # Extract legal refs + convert context items for pill display
                legal_refs_for_display, legal_refs_v3 = extract_legal_refs_for_display(v3_response, context_items)
                v1_chunks_for_display = context_items_to_v1_chunks(context_items, Chunk)

                if (
                    (v1_chunks_for_display or legal_refs_for_display)
                    and not is_negative_response(v3_response)
                    and not should_hide_sources(v3_response)
                ):
                    render_sources(v1_chunks_for_display, key_suffix="current_v3", legal_refs=legal_refs_for_display or None)

                # Beta-excluded themes warning (uncomment when validated by project lead)
                # if qr_v3.theme and qr_v3.theme in BETA_EXCLUDED_THEMES:
                #     _theme_labels = {"action_sociale": "Action Sociale", "psc": "PSC (Protection Sociale Complémentaire)", "retraite": "Retraite", "apprentis": "Apprentis"}
                #     st.warning(
                #         f"La thématique **{_theme_labels.get(qr_v3.theme, qr_v3.theme)}** est actuellement hors périmètre du beta-test. "
                #         "La réponse ci-dessus est indicative et peut être incomplète ou imprécise sur ce sujet."
                #     )

                # Afficher les stats en mode verbose
                if rag_config.verbose_mode:
                    selector_info = ""
                    _sel_before = v3_metadata.get("selector_items_before", 0)
                    _sel_after = v3_metadata.get("selector_items_after", 0)
                    if v3_enable_selector and _sel_before > 0:
                        selector_info = f" | Selector: {_sel_after}/{_sel_before} kept"
                    acr_info = ""
                    if qr_v3.detected_acronyms:
                        acr_info = f" | Acronymes: {dict(qr_v3.detected_acronyms)} → expand: {qr_v3.expanded_acronyms}"
                    legal_info = f" | DGAFP: {'oui' if qr_v3.needs_legal_search else 'non'}"
                    tables_searched = v3_metadata.get("tables_searched", [])
                    tables_info = f" | Tables: {', '.join(tables_searched)}" if tables_searched else ""
                    st.caption(
                        f"⏱️ RAG V3 - {total_time_ms:.0f}ms | Mode: {v3_context_mode} | {len(context_items)} items | {total_tokens} tokens{selector_info}{legal_info}{tables_info}{acr_info}"
                    )

            # ═══════════════════════════════════════════════════════════════════
            # 📊 Créer le Turn pour l'historique
            # ═══════════════════════════════════════════════════════════════════
            turn_obj = Turn(
                id=turn_id,
                user=query,
                assistant=v3_response,
                retrieved=v1_chunks_for_display,  # Maintenant on a les chunks
                prompt_used="",
                legal_refs=legal_refs_for_display if legal_refs_for_display else [],  # 🔧 Stocker les refs matchées (MatchedReference) pas les brutes
            )
            st.session_state.turns.append(turn_obj)

            # ═══════════════════════════════════════════════════════════════════
            # Log to DB/CSV avec les vraies métadonnées
            # ═══════════════════════════════════════════════════════════════════
            try:
                row = build_log_row(
                    turn_id=turn_id,
                    query=query,
                    response=v3_response,
                    pipeline=pipeline_v3,
                    qr=qr_v3,
                    config=config_v3,
                    runtime_config=rag_config,
                    session_state=dict(st.session_state),
                    total_time_ms=total_time_ms,
                    context_items=context_items,
                    v1_chunks_for_display=v1_chunks_for_display,
                    legal_refs_v3=legal_refs_v3,
                    trace_id=trace_id,
                )
                log_run_row(row)
                log_trace_event_rows(v3_metadata.get("rag_trace_events", []), turn_id=turn_id, trace_id=trace_id)
            except Exception as e:
                import logging as _logging
                import traceback

                _logging.error("Log V3 failed: %s\n%s", e, traceback.format_exc())
                st.toast(f"⚠️ Log DB échoué: {type(e).__name__}: {e}", icon="⚠️")

            # Afficher le bloc feedback
            with st.chat_message("assistant"):
                render_feedback_block(turn_obj)

            # STOP - Ne pas continuer vers V1
            st.stop()

        except Exception:
            import logging
            import traceback

            logging.getLogger(__name__).error("RAG V3 pipeline error:\n%s", traceback.format_exc())
            st.error("Une erreur est survenue lors du traitement de votre question. Veuillez réessayer.")
            st.stop()


with st.bottom:
    st.caption(
        # body='<p style="text-align: center;"><i>Les réponses fournies peuvent contenir des inexactitudes. Vérifiez systématiquement les sources avant toute décision.</i></p>',
        body='<p style="text-align: center;"><i>Les réponses générées par l’IA doivent être relues, complétées et validées avant toute utilisation et diffusion auprès des agents.</i></p>',
        unsafe_allow_html=True,
    )
