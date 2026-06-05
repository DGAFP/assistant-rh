"""Chat Logs viewer – reads from PostgreSQL with CSV fallback."""
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.cookies_security import is_production_like_env
from src.ui.db_utils import get_engine

st.set_page_config(page_title="Chat Logs", page_icon="📕", layout="wide")

require_admin()
show_admin_badge()

engine = get_engine()

# ---------- CSV paths (fallback) ----------
# In production, use /tmp to avoid triggering file watcher
_IS_PRODUCTION = is_production_like_env()
if _IS_PRODUCTION:
    BASE = Path("/tmp/assistant_rh_data")
else:
    BASE = Path(__file__).resolve().parent.parent / "data"
RUNS_CSV = BASE / "chat_runs" / "chat_runs.csv"
FEEDS_CSV = BASE / "chat_feedbacks" / "chat_feedbacks.csv"

def _read_csv(path: Path) -> pd.DataFrame:
    """Helper to read CSV safely."""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as e:
        st.error(f"Erreur lecture {path.name}: {e}")
        return pd.DataFrame()

def _compute_context_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Derive v3_context_distribution, v3_context_titles, and v3_doc_entire_titles from v3_context_items_summary."""
    distributions = []
    titles_list = []
    doc_entire_list = []
    for _, row in df.iterrows():
        raw = row.get("v3_context_items_summary")
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str) and raw.startswith("["):
            try:
                items = json.loads(raw)
            except Exception:
                items = []

        pub_counts: dict[str, int] = {}
        for it in items:
            pub = it.get("publisher") or "?"
            pub_counts[pub] = pub_counts.get(pub, 0) + 1

        titles = []
        doc_entire_titles = []
        for it in items:
            heading = (it.get("heading") or "?")[:60]
            pub = it.get("publisher") or ""
            titles.append(f"{heading} ({pub})" if pub else heading)
            if it.get("is_doc_entire"):
                doc_entire_titles.append(f"{heading} ({pub})" if pub else heading)

        distributions.append(json.dumps(pub_counts, ensure_ascii=False) if pub_counts else "")
        titles_list.append(" | ".join(titles) if titles else "")
        doc_entire_list.append(" | ".join(doc_entire_titles) if doc_entire_titles else "")

    df["v3_context_distribution"] = distributions
    df["v3_context_titles"] = titles_list
    df["v3_doc_entire_titles"] = doc_entire_list
    return df


def _clean_distribution(val):
    """Remove null/zero entries from a source distribution dict."""
    if val is None:
        return None
    d = val
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except (json.JSONDecodeError, TypeError):
            return val
    if isinstance(d, dict):
        cleaned = {k: v for k, v in d.items() if v}
        return cleaned if cleaned else None
    return val


# ---------- Load merged dataframe (runs + latest feedback + review) ----------
@st.cache_data(ttl=10)
def load_merged() -> pd.DataFrame:
    """Load chat logs from PostgreSQL or CSV fallback."""
    
    # Try PostgreSQL first
    if engine:
        try:
            sql = text("""
                WITH last_fb AS (
                  SELECT DISTINCT ON (turn_id)
                         turn_id, helpful, reasons, comment, stars, ts
                  FROM chat_feedbacks
                  ORDER BY turn_id, ts DESC
                )
                SELECT
                  -- Core
                  r.ts, r.turn_id, r.session_id, r.conversation_id, r.turn_index,
                  r.question, r.answer, r.model, r.temperature,
                  r.top_k, r.total_time_ms, r.dist_after_rerank,
                  r.rag_version, r.expanded_refs_count, r.user_group,
                  -- LLM Selector tracking
                  r.llm_selector_model, r.llm_selector_prompt_name, r.llm_selector_reasoning,
                  r.intent_gating_prompt_name,
                  -- V3 observability: context
                  r.v3_context_mode, r.v3_sections_count, r.v3_context_items_count,
                  r.v3_context_tokens, r.v3_doc_entire_count,
                  -- V3 observability: selector
                  r.v3_selector_confidence, r.v3_selector_selected_count,
                  r.v3_needs_legal_llm,
                  -- V3 observability: legal refs
                  r.v3_legal_refs_total, r.v3_legal_refs_from_expansion, r.v3_legal_refs_from_dgafp,
                  r.v3_legal_refs_details,
                  -- V3 observability: retrieval
                  r.v3_chunks_retrieved_count, r.v3_embedding_model, r.v3_search_mode,
                  r.v3_reranker_enabled, r.v3_rerank_top_k,
                  -- V3 observability: query processing
                  r.v3_intent, r.v3_intent_gating_enabled, r.v3_should_proceed, r.v3_acronyms_expanded,
                  -- V3 observability: debug / JSONB
                  r.v3_context_items_summary, r.v3_source_distribution,
                  -- V3 observability: detailed
                  r.v3_generator_prompt_name,
                  r.v3_intent_llm_response, r.v3_selector_llm_response,
                  r.v3_detected_theme, r.v3_reformulated_query,
                  r.v3_context_items_full,
                  r.v3_selector_kept_indices, r.v3_selector_removed_indices, r.v3_selector_decisions,
                  r.v3_sections_before_rerank, r.v3_sections_after_rerank,
                  r.v3_full_prompt, r.v3_system_prompt_content,
                  -- V3 observability: timing
                  r.v3_query_processing_ms, r.v3_retrieval_ms, r.v3_aggregation_ms,
                  r.v3_selector_ms, r.v3_context_building_ms, r.v3_generation_ms,
                  -- V3 observability: LLM metrics
                  r.v3_ttft_ms, r.v3_chars_per_second, r.v3_response_length,
                  -- Feedback
                  f.helpful, f.reasons, f.comment, f.stars
                FROM chat_runs r
                LEFT JOIN last_fb f USING (turn_id)
                ORDER BY r.ts DESC
            """)
            df = pd.read_sql_query(sql, engine)
            # Ensure timezone-aware
            if "ts" in df.columns:
                df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Paris")
            # Convert stars from 0-4 to 1-5 for display
            if "stars" in df.columns:
                df["stars"] = df["stars"].apply(lambda x: x + 1 if pd.notna(x) else x)
            # Compute derived context columns from JSONB before serialization
            df = _compute_context_columns(df)

            # Clean distribution columns: remove null/zero entries
            for dist_col in ("dist_after_rerank", "v3_source_distribution"):
                if dist_col in df.columns:
                    df[dist_col] = df[dist_col].apply(_clean_distribution)

            # Convert JSONB columns to strings for PyArrow compatibility
            jsonb_cols = ["dist_after_rerank", "v3_source_distribution",
                          "v3_legal_refs_details", "v3_context_items_summary",
                          "v3_context_items_full", "v3_selector_decisions",
                          "v3_sections_before_rerank", "v3_sections_after_rerank"]
            for col in jsonb_cols:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list))
                        else str(x) if x is not None else None
                    )
            return df
        except Exception as e:
            st.warning(f"Erreur PostgreSQL, fallback vers CSV: {e}")
    
    # Fallback to CSV (local development)
    runs = _read_csv(RUNS_CSV)
    fb = _read_csv(FEEDS_CSV)
    
    if runs.empty:
        return runs
    
    # Merge feedback (latest per turn_id)
    if not fb.empty and "turn_id" in fb.columns:
        fb["ts"] = pd.to_datetime(fb["ts"], errors="coerce")
        fb = fb.sort_values("ts").groupby("turn_id", as_index=False).tail(1)
        # Include stars if available (convert 0-4 to 1-5 for display)
        fb_cols = ["turn_id", "helpful", "reasons", "comment"]
        if "stars" in fb.columns:
            fb["stars"] = fb["stars"].apply(lambda x: x + 1 if pd.notna(x) else x)
            fb_cols.append("stars")
        runs = runs.merge(
            fb[fb_cols],
            how="left", on="turn_id"
        )
    
    # Sort by timestamp descending
    if "ts" in runs.columns:
        runs["ts"] = pd.to_datetime(runs["ts"], errors="coerce")
        runs = runs.sort_values("ts", ascending=False)
    
    return runs

# ---------- UI ----------
st.title("📕 Chat Logs")

with st.sidebar:
    st.markdown("### ⚙️ Options")
    if st.button("🔄 Rafraîchir les données", width="stretch", type="primary"):
        load_merged.clear()
        st.rerun()
    st.caption("Les données sont mises en cache 10s.")
    st.divider()
    include_legacy = st.checkbox("Inclure les anciens runs (V1/V2)", value=False)

_ = get_engine()

df = load_merged()
if df.empty:
    st.info("Aucun log pour le moment.")
    st.stop()

# Default to V3 runs only
view = df.copy()
if not include_legacy and "rag_version" in view.columns:
    view = view[view["rag_version"] == "v3"]

# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE STAGE COLUMN GROUPS
# ═══════════════════════════════════════════════════════════════════════════════

BASE_COLUMNS = [
    "ts", "user_group", "session_id", "conversation_id", "turn_index",
    "question", "answer", "stars",
]

STAGE_GROUPS = {
    "🧠 Intent": [
        "v3_intent", "v3_detected_theme", "v3_should_proceed",
        "v3_needs_legal_llm",
        "v3_acronyms_expanded", "v3_reformulated_query",
        "v3_intent_llm_response",
    ],
    "🔍 Retrieval": [
        "v3_search_mode", "v3_embedding_model", "top_k",
        "v3_chunks_retrieved_count",
        "v3_reranker_enabled",
        "v3_sections_before_rerank", "v3_sections_after_rerank",
        "dist_after_rerank",
    ],
    "🎯 Selection": [
        "llm_selector_model", "v3_selector_confidence",
        "v3_selector_selected_count", "v3_selector_decisions",
        "llm_selector_reasoning",
        "v3_source_distribution",
    ],
    "📄 Context": [
        "v3_context_mode", "v3_context_items_count",
        "v3_context_distribution", "v3_context_titles",
        "v3_context_tokens", "v3_doc_entire_count", "v3_doc_entire_titles",
    ],
    "⚖️ Réfs juridiques": [
        "v3_legal_refs_total", "v3_legal_refs_from_dgafp",
        "v3_legal_refs_details",
    ],
    "✍️ Generation": [
        "model", "v3_generator_prompt_name", "temperature",
        "v3_ttft_ms", "v3_response_length",
    ],
    "⏱️ Timing": [
        "v3_query_processing_ms", "v3_retrieval_ms", "v3_aggregation_ms",
        "v3_selector_ms", "v3_context_building_ms", "v3_generation_ms", "total_time_ms",
    ],
    "💬 Feedback": [
        "reasons", "comment",
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
# FILTERS & TOGGLES
# ═══════════════════════════════════════════════════════════════════════════════

q = st.text_input("🔎 Filtrer", placeholder="Rechercher dans toutes les colonnes...", label_visibility="collapsed")
period = st.pills("📅 Période", ["Tout", "Aujourd'hui", "Hier", "Cette semaine"], default="Tout", label_visibility="collapsed")

active_stages = st.pills(
    "📊 Étapes du pipeline",
    list(STAGE_GROUPS.keys()),
    default=[],
    selection_mode="multi",
    label_visibility="collapsed",
)

# Build column_order dynamically
column_order = list(BASE_COLUMNS)
for stage in (active_stages or []):
    column_order.extend(STAGE_GROUPS[stage])

# Text filter
if q:
    qlower = q.lower()
    def _match(row):
        bag = " ".join(str(v) for c, v in row.items() if c != "turn_id").lower()
        return qlower in bag
    view = view[view.apply(_match, axis=1)]

# Period filter
if period and "ts" in view.columns:
    now = pd.Timestamp.now(tz="Europe/Paris")
    if period == "Aujourd'hui":
        view = view[view["ts"] >= now.normalize()]
    elif period == "Hier":
        yesterday_start = (now - timedelta(days=1)).normalize()
        view = view[(view["ts"] >= yesterday_start) & (view["ts"] < now.normalize())]
    elif period == "Cette semaine":
        week_start = (now - timedelta(days=now.weekday())).normalize()
        view = view[view["ts"] >= week_start]

st.caption(f"📊 {len(view)} ligne(s)")

# ═══════════════════════════════════════════════════════════════════════════════
# DATAFRAME DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════

st.dataframe(
    view,
    width="stretch",
    hide_index=True,
    height=600,
    column_order=[c for c in column_order if c in view.columns],
    column_config={
        # Base
        "ts": st.column_config.DatetimeColumn("Date/heure", format="YYYY-MM-DD HH:mm"),
        "user_group": st.column_config.TextColumn("Groupe", width="small"),
        "session_id": st.column_config.TextColumn("Session", width="small"),
        "conversation_id": st.column_config.TextColumn("Conversation", width="small"),
        "turn_index": st.column_config.NumberColumn("Tour #", format="%d"),
        "question": st.column_config.TextColumn("Question", width="medium"),
        "answer": st.column_config.TextColumn("Réponse", width="large"),
        "stars": st.column_config.NumberColumn("⭐", format="%d ⭐", help="Note 1-5"),
        # Intent
        "v3_intent": st.column_config.TextColumn("Intent", width="small", help="rag_query / chit_chat / out_of_scope"),
        "v3_detected_theme": st.column_config.TextColumn("Thème", width="small", help="Thème RH détecté"),
        "v3_should_proceed": st.column_config.CheckboxColumn("Proceed?", help="Le pipeline a-t-il continué"),
        "v3_needs_legal_llm": st.column_config.CheckboxColumn("DGAFP?", help="L'intent gater a détecté un besoin de recherche juridique (DGAFP)"),
        "v3_acronyms_expanded": st.column_config.TextColumn("Acronymes", width="small"),
        "v3_reformulated_query": st.column_config.TextColumn("Query reformulée", width="small"),
        "v3_intent_llm_response": st.column_config.TextColumn("Intent (JSON)", width="large", help="Réponse brute du LLM intent"),
        # Retrieval
        "v3_search_mode": st.column_config.TextColumn("Search mode", width="small", help="semantic / hybrid / lexical"),
        "v3_embedding_model": st.column_config.TextColumn("Embedding", width="small"),
        "top_k": st.column_config.NumberColumn("Top K / table", format="%d", help="Nombre de chunks récupérés par table"),
        "v3_chunks_retrieved_count": st.column_config.NumberColumn("Total chunks", format="%d", help="Total chunks récupérés (top_k × nb tables)"),
        "v3_reranker_enabled": st.column_config.CheckboxColumn("Reranker?"),
        "v3_sections_before_rerank": st.column_config.TextColumn("Sections (avant)", width="small", help="Nombre de sections avant reranking"),
        "v3_sections_after_rerank": st.column_config.TextColumn("Sections (après)", width="small", help="Nombre de sections après reranking = entrée du Selector"),
        "dist_after_rerank": st.column_config.TextColumn("Distrib. post-rerank", width="medium", help="Distribution par source après reranking (= entrée du Selector)"),
        # Selection
        "llm_selector_model": st.column_config.TextColumn("Selector model", width="small"),
        "v3_selector_confidence": st.column_config.NumberColumn("Taux sélection", format="%.2f", help="Ratio items gardés / items reçus par le Selector (1.0 = selector off ou fallback)"),
        "v3_selector_selected_count": st.column_config.NumberColumn("Sélectionnés", format="%d", help="Sections gardées par le Selector (sur total post-rerank)"),
        "v3_selector_decisions": st.column_config.TextColumn("Décisions", width="large", help="JSON kept/removed avec indices et raison"),
        "llm_selector_reasoning": st.column_config.TextColumn("Raisonnement", width="medium", help="Raison résumée du Selector"),
        # Context
        "v3_context_mode": st.column_config.TextColumn("Mode", width="small", help="narrow / standard / wide"),
        "v3_context_items_count": st.column_config.NumberColumn("Items finaux", format="%d", help="Nombre d'items après ContextBuilder (budget tokens)"),
        "v3_context_distribution": st.column_config.TextColumn("Distribution", width="medium", help="Répartition docs / sections dans le contexte final"),
        "v3_context_titles": st.column_config.TextColumn("Titres items", width="large", help="Titre et source de chaque item du contexte final"),
        "v3_context_tokens": st.column_config.NumberColumn("Context tokens", format="%d", help="Nombre de tokens dans le contexte envoyé au LLM"),
        "v3_doc_entire_count": st.column_config.NumberColumn("Docs entiers", format="%d", help="Nombre de documents inclus entièrement"),
        "v3_doc_entire_titles": st.column_config.TextColumn("Docs entiers (titres)", width="large", help="Titres des items inclus en document entier"),
        "v3_source_distribution": st.column_config.TextColumn("Distrib. post-selector", width="medium", help="Distribution des sources après filtrage LLM Selector (avant budget)"),
        # Legal refs
        "v3_legal_refs_total": st.column_config.NumberColumn("Réfs citées", format="%d", help="Nombre de références juridiques mentionnées dans les sections du contexte"),
        "v3_legal_refs_from_dgafp": st.column_config.NumberColumn("Réfs injectées", format="%d", help="Références résolues depuis rag_chunks_dgafp et injectées dans le contexte (≤ citées)"),
        "v3_legal_refs_details": st.column_config.TextColumn("Détail réfs injectées", width="large", help="Références résolues depuis rag_chunks_dgafp : numéro, CID et titre"),
        # Generation
        "model": st.column_config.TextColumn("Modèle LLM", width="small"),
        "v3_generator_prompt_name": st.column_config.TextColumn("Prompt", width="small"),
        "temperature": st.column_config.NumberColumn("Temp.", format="%.1f"),
        "v3_ttft_ms": st.column_config.NumberColumn("TTFT (ms)", format="%d", help="Time to first token"),
        "v3_response_length": st.column_config.NumberColumn("Réponse (tokens)", format="%d", help="Estimation tokens de la réponse (len/4)"),
        # Timing
        "v3_query_processing_ms": st.column_config.NumberColumn("Intent (ms)", format="%d", help="Intent gater + expansion acronymes"),
        "v3_retrieval_ms": st.column_config.NumberColumn("Retrieval (ms)", format="%d", help="Recherche sémantique/hybride sur les tables"),
        "v3_aggregation_ms": st.column_config.NumberColumn("Agrég.+Rerank (ms)", format="%d", help="Agrégation chunks→sections + reranking"),
        "v3_selector_ms": st.column_config.NumberColumn("Selector (ms)", format="%d", help="Filtrage LLM Selector"),
        "v3_context_building_ms": st.column_config.NumberColumn("ContextBuild (ms)", format="%d", help="Budget tokens, doc-entier, triangulation, injection réfs juridiques"),
        "v3_generation_ms": st.column_config.NumberColumn("Génération (ms)", format="%d", help="Génération LLM (streaming)"),
        "total_time_ms": st.column_config.NumberColumn("Total (ms)", format="%d", help="Temps wall-clock total du pipeline (début requête → fin réponse)"),
        # Feedback
        "reasons": st.column_config.TextColumn("Motifs", width="medium"),
        "comment": st.column_config.TextColumn("Commentaire", width="medium"),
    },
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION: Métriques de Performance (V3 pipeline)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v3_df = view[view["rag_version"] == "v3"].copy() if "rag_version" in view.columns else pd.DataFrame()

if v3_df.empty:
    st.stop()

st.divider()
st.subheader("📊 Métriques Pipeline V3")

V3_TIMING_COLS = [
    "v3_query_processing_ms", "v3_retrieval_ms", "v3_aggregation_ms",
    "v3_selector_ms", "v3_context_building_ms", "v3_generation_ms", "total_time_ms",
]
for col in V3_TIMING_COLS + ["v3_ttft_ms", "v3_chars_per_second",
                              "v3_chunks_retrieved_count", "v3_sections_count",
                              "v3_context_items_count", "v3_context_tokens",
                              "v3_selector_confidence", "v3_selector_selected_count",
                              "v3_legal_refs_total", "v3_legal_refs_from_dgafp",
                              "v3_doc_entire_count", "v3_response_length"]:
    if col in v3_df.columns:
        v3_df[col] = pd.to_numeric(v3_df[col], errors="coerce")


def _fmt_time(ms: float) -> str:
    if pd.isna(ms):
        return "—"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


st.caption(f"Basé sur {len(v3_df)} run(s) V3")

# ═══════════════════════════════════════════════════════════════
# ROW 1: Latence du pipeline
# ═══════════════════════════════════════════════════════════════
st.markdown("#### Latence du pipeline")

perf = v3_df.dropna(subset=["total_time_ms"])
if "v3_retrieval_ms" in perf.columns:
    perf = perf[perf["v3_retrieval_ms"] > 0]
if len(perf) > 0:
    st.caption(f"Sur {len(perf)} run(s) avec timing détaillé")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    _steps = [
        (col1, "v3_query_processing_ms", "Intent"),
        (col2, "v3_retrieval_ms", "Retrieval"),
        (col3, "v3_aggregation_ms", "Agrég.+Rerank"),
        (col4, "v3_selector_ms", "Selector"),
        (col5, "v3_context_building_ms", "Context Build"),
        (col6, "v3_generation_ms", "Génération"),
    ]
    for col, key, label in _steps:
        if key in perf.columns:
            active = perf[perf[key] > 0][key] if key in ("v3_selector_ms", "v3_context_building_ms") else perf[key]
            med = active.median() if len(active) > 0 else 0
            n = len(active)
            col.metric(label, _fmt_time(med), help=f"Médiane sur {n} runs actifs")

    total_med = perf["total_time_ms"].median()
    total_min = perf["total_time_ms"].min()
    total_max = perf["total_time_ms"].max()
    st.metric("Total (médiane)", _fmt_time(total_med))
    st.caption(f"Min: {_fmt_time(total_min)} — Max: {_fmt_time(total_max)}")

    total_avg = perf["total_time_ms"].mean()
    if total_avg > 0:
        parts = []
        for _, key, label in _steps:
            if key in perf.columns:
                pct = (perf[key].mean() / total_avg) * 100
                parts.append(f"{label} {pct:.0f}%")
        st.caption("Répartition: " + " | ".join(parts))

    col_ttft, col_cps = st.columns(2)
    if "v3_ttft_ms" in perf.columns:
        ttft_data = perf.dropna(subset=["v3_ttft_ms"])
        ttft_data = ttft_data[ttft_data["v3_ttft_ms"] > 0]
        if len(ttft_data) > 0:
            med_ttft = ttft_data["v3_ttft_ms"].median()
            col_ttft.metric("Time to First Token", _fmt_time(med_ttft),
                            help="Latence perçue par l'utilisateur")
            col_ttft.caption(f"Min: {_fmt_time(ttft_data['v3_ttft_ms'].min())} — Max: {_fmt_time(ttft_data['v3_ttft_ms'].max())}")
    if "v3_chars_per_second" in perf.columns:
        cps_data = perf.dropna(subset=["v3_chars_per_second"])
        cps_data = cps_data[cps_data["v3_chars_per_second"] > 0]
        if len(cps_data) > 0:
            med_cps = cps_data["v3_chars_per_second"].median()
            col_cps.metric("Throughput", f"{med_cps:.0f} chars/s",
                           help="Débit de génération (médiane)")
            col_cps.caption(f"Min: {cps_data['v3_chars_per_second'].min():.0f} — Max: {cps_data['v3_chars_per_second'].max():.0f} chars/s")
else:
    st.info("Aucune donnée de timing disponible.")

# ═══════════════════════════════════════════════════════════════
# ROW 2: Vue d'ensemble Pipeline
# ═══════════════════════════════════════════════════════════════
st.divider()
st.markdown("#### Vue d'ensemble du pipeline")

col1, col2, col3, col4 = st.columns(4)

with col1:
    if "v3_chunks_retrieved_count" in v3_df.columns:
        st.metric("Chunks récupérés (moy)", f"{v3_df['v3_chunks_retrieved_count'].mean():.1f}")
with col2:
    if "v3_sections_count" in v3_df.columns:
        st.metric("Sections agrégées (moy)", f"{v3_df['v3_sections_count'].mean():.1f}")
with col3:
    if "v3_context_items_count" in v3_df.columns:
        st.metric("Items contexte (moy)", f"{v3_df['v3_context_items_count'].mean():.1f}")
with col4:
    if "v3_context_tokens" in v3_df.columns:
        st.metric("Tokens contexte (moy)", f"{v3_df['v3_context_tokens'].mean():.0f}")

# ═══════════════════════════════════════════════════════════════
# ROW 3: LLM Selector
# ═══════════════════════════════════════════════════════════════
st.divider()
st.markdown("#### LLM Selector")

col1, col2, col3 = st.columns(3)
with col1:
    if "v3_selector_confidence" in v3_df.columns:
        mean_conf = v3_df["v3_selector_confidence"].mean()
        min_conf = v3_df["v3_selector_confidence"].min()
        st.metric("Taux sélection (moy)", f"{mean_conf:.0%}")
        st.caption(f"Min: {min_conf:.0%}")
with col2:
    if "v3_selector_selected_count" in v3_df.columns:
        mean_sel = v3_df["v3_selector_selected_count"].mean()
        st.metric("Sections sélectionnées (moy)", f"{mean_sel:.1f}")
with col3:
    if "v3_doc_entire_count" in v3_df.columns:
        total_docs_entire = int(v3_df["v3_doc_entire_count"].sum())
        mean_docs = v3_df["v3_doc_entire_count"].mean()
        st.metric("Docs entiers (total)", total_docs_entire)
        st.caption(f"Moy: {mean_docs:.1f} par run")

# ═══════════════════════════════════════════════════════════════
# ROW 4: Réfs juridiques
# ═══════════════════════════════════════════════════════════════
st.divider()
st.markdown("#### Références juridiques")

col1, col2, col3 = st.columns(3)
with col1:
    if "v3_needs_legal_llm" in v3_df.columns:
        legal_requested = v3_df["v3_needs_legal_llm"].sum()
        st.metric("Recherche DGAFP demandée", f"{int(legal_requested)} / {len(v3_df)} runs")
with col2:
    if "v3_legal_refs_total" in v3_df.columns:
        total_cited = int(v3_df["v3_legal_refs_total"].sum())
        mean_cited = v3_df["v3_legal_refs_total"].mean()
        st.metric("Réfs citées (total)", total_cited)
        st.caption(f"Moy: {mean_cited:.1f} par run")
with col3:
    if "v3_legal_refs_from_dgafp" in v3_df.columns:
        total_injected = int(v3_df["v3_legal_refs_from_dgafp"].sum())
        mean_injected = v3_df["v3_legal_refs_from_dgafp"].mean()
        st.metric("Réfs injectées (total)", total_injected)
        st.caption(f"Moy: {mean_injected:.1f} par run")

# ═══════════════════════════════════════════════════════════════
# ROW 5: Distribution des Sources
# ═══════════════════════════════════════════════════════════════
st.divider()
st.markdown("#### Distribution des sources (post-Selector)")

if "v3_source_distribution" in v3_df.columns:
    source_totals: dict[str, int] = {}
    for dist_str in v3_df["v3_source_distribution"].dropna():
        try:
            dist = json.loads(dist_str) if isinstance(dist_str, str) else dist_str
            if isinstance(dist, dict):
                for source, count in dist.items():
                    source_totals[source] = source_totals.get(source, 0) + int(count)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if source_totals:
        col1, col2 = st.columns([2, 1])
        source_chart = pd.DataFrame(list(source_totals.items()), columns=["Source", "Count"])
        source_chart = source_chart.sort_values("Count", ascending=False)
        col1.bar_chart(source_chart.set_index("Source"))
        col2.dataframe(source_chart, hide_index=True, width="stretch")
    else:
        st.info("Pas de données de distribution disponibles.")
else:
    st.info("Colonne v3_source_distribution non disponible.")

# ═══════════════════════════════════════════════════════════════
# INSPECTION DETAILLEE
# ═══════════════════════════════════════════════════════════════
with st.expander("Inspection détaillée d'un run", expanded=False):
    run_options = v3_df[["ts", "question", "turn_id"]].head(15).copy()
    run_options["label"] = run_options["ts"].astype(str).str[:16] + " — " + run_options["question"].str[:100]

    selected_run = st.selectbox(
        "Run à inspecter",
        options=run_options["turn_id"].tolist(),
        format_func=lambda x: run_options[run_options["turn_id"] == x]["label"].values[0] if x in run_options["turn_id"].values else x,
        key="v3_inspect_run",
    )

    if selected_run:
        run = v3_df[v3_df["turn_id"] == selected_run].iloc[0]

        tab_overview, tab_context, tab_selector, tab_prompt = st.tabs([
            "Overview", "Context Items", "Selector", "Full Prompt",
        ])

        with tab_overview:
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Intent", str(run.get("v3_intent", ""))[:30])
                st.metric("Thème", run.get("v3_detected_theme") or "—")
                st.metric("DGAFP?", "Oui" if run.get("v3_needs_legal_llm") else "Non")
            with col2:
                st.metric("Chunks récupérés", run.get("v3_chunks_retrieved_count", 0))
                st.metric("Sections sélectionnées", run.get("v3_selector_selected_count", 0))
                st.metric("Items finaux", run.get("v3_context_items_count", 0))
            with col3:
                st.metric("Taux sélection", f"{run.get('v3_selector_confidence', 0):.0%}")
                st.metric("Tokens contexte", run.get("v3_context_tokens", 0))
                st.metric("Docs entiers", run.get("v3_doc_entire_count", 0))

            if run.get("v3_reformulated_query"):
                st.info(f"**Query reformulée:** {run['v3_reformulated_query']}")
            if run.get("v3_acronyms_expanded"):
                st.info(f"**Acronymes:** {run['v3_acronyms_expanded']}")

            st.markdown("**Prompts utilisés:**")
            st.json({
                "Intent": run.get("intent_gating_prompt_name") or "—",
                "Selector": run.get("llm_selector_prompt_name") or "—",
                "Generator": run.get("v3_generator_prompt_name") or "—",
            })

            st.markdown("**Timing (ms):**")
            timing_row = {}
            for key, label in [
                ("v3_query_processing_ms", "Intent"), ("v3_retrieval_ms", "Retrieval"),
                ("v3_aggregation_ms", "Agrég.+Rerank"), ("v3_selector_ms", "Selector"),
                ("v3_context_building_ms", "Context Build"), ("v3_generation_ms", "Génération"),
                ("total_time_ms", "TOTAL"),
            ]:
                val = run.get(key, 0)
                timing_row[label] = _fmt_time(val) if pd.notna(val) else "—"
            st.json(timing_row)

        with tab_context:
            st.markdown("**Context items (avec métadonnées):**")
            raw = run.get("v3_context_items_full") or run.get("v3_context_items_summary")
            if raw:
                try:
                    items = json.loads(raw) if isinstance(raw, str) else raw
                    st.dataframe(pd.DataFrame(items), width="stretch", hide_index=True)
                except Exception:
                    st.code(str(raw))
            else:
                st.info("Aucun détail de contexte disponible.")

        with tab_selector:
            col1, col2 = st.columns(2)
            kept = run.get("v3_selector_kept_indices", "")
            removed = run.get("v3_selector_removed_indices", "")
            col1.success(f"Indices conservés: {kept if kept else '—'}")
            col2.error(f"Indices retirés: {removed if removed else '—'}")

            st.markdown("**Raisonnement:**")
            st.info(run.get("llm_selector_reasoning") or "—")

            raw_sel = run.get("v3_selector_llm_response")
            if raw_sel:
                with st.expander("Réponse LLM brute"):
                    st.code(str(raw_sel), language="json")

            decisions_raw = run.get("v3_selector_decisions")
            if decisions_raw:
                with st.expander("Décisions structurées"):
                    try:
                        dec = json.loads(decisions_raw) if isinstance(decisions_raw, str) else decisions_raw
                        st.json(dec)
                    except Exception:
                        st.code(str(decisions_raw))

        with tab_prompt:
            sys_prompt = run.get("v3_system_prompt_content")
            if sys_prompt:
                with st.expander("System Prompt", expanded=False):
                    st.code(str(sys_prompt), language="markdown")

            full_prompt = run.get("v3_full_prompt")
            if full_prompt:
                with st.expander("User Prompt (context + question)", expanded=True):
                    prompt_str = str(full_prompt)
                    st.code(prompt_str[:10000], language="markdown")
                    if len(prompt_str) > 10000:
                        st.warning("Tronqué à 10k caractères.")
            else:
                st.info("Full prompt non disponible.")