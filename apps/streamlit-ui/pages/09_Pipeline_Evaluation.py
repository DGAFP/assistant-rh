"""
🔬 Pipeline Ablation Evaluation — Mesure l'impact de chaque module du pipeline V3

Compare N configurations du pipeline V3 sur un goldset filtré par tags/goldset_name.
Utilise un pipeline d'évaluation léger (pas d'intent gating, pas de legal refs)
pour une exécution rapide tout en testant les vrais modules de retrieval.
"""

import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st
from assistant_rh_rag_pipeline import RAGConfig as RAGConfigV3
from assistant_rh_rag_pipeline import create_pipeline
from assistant_rh_rag_pipeline.config import (
    EmbeddingModel,
    QueryProcessorConfig,
    RetrievalConfig,
    SearchMode,
    SectionAggregationConfig,
    SelectorConfig,
)
from assistant_rh_rag_pipeline.config import (
    get_acronym_dict as _get_acro,
)
from assistant_rh_rag_pipeline.context_selector import ContextSelector
from assistant_rh_rag_pipeline.db_helpers import get_dsn as get_app_dsn
from assistant_rh_rag_pipeline.ministry_scope import (
    MINISTRY_CATALOG,
    build_retrieval_scope,
    resolve_ministry,
)
from dotenv import load_dotenv
from psycopg.rows import dict_row

from src.pipeline_eval_helpers import final_items_for_metrics, selector_variant_label
from src.ui.admin_auth import require_admin, show_admin_badge

load_dotenv()

require_admin()
show_admin_badge()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Pipeline Evaluation", page_icon="🔬", layout="wide")
st.title("🔬 Pipeline Ablation Evaluation")
st.caption("Mesure chaque module sur la première passe — le rejeu Suivi-Tests (#298) couvre le retry et la parité production")


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════


def get_dsn() -> str:
    tunnel_dsn = os.getenv("TUNNEL_DSN")
    if tunnel_dsn:
        return tunnel_dsn
    try:
        return get_app_dsn()
    except RuntimeError:
        st.error("Aucune connexion DB configurée")
        st.stop()


DSN = get_dsn()


def parse_gold_sources(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def format_gold_sources(value) -> str:
    return ", ".join(parse_gold_sources(value))


def gold_sources_match(value, short_id: str) -> bool:
    short_id_key = str(short_id or "").strip().lower()
    if not short_id_key:
        return False
    return short_id_key in {source.lower() for source in parse_gold_sources(value)}


def get_connection():
    conn = st.session_state.get("_pipe_eval_conn")
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            st.session_state["_pipe_eval_conn"] = None

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(1 * attempt)
            conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
            st.session_state["_pipe_eval_conn"] = conn
            return conn
        except Exception as e:
            if attempt == 2:
                raise e
    raise RuntimeError("Could not connect to DB")


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

EMBEDDING_OPTIONS = {
    "Albert Raw (embedding_raw)": "albert_raw",
    "Albert Ctx (embedding)": "albert_ctx",
    "Albert Raw Text (embedding_raw_text)": "albert_raw_text",
    "BGE Scaleway (embedding_bge)": "bge_scaleway",
}

SEARCH_MODE_OPTIONS = ["semantic", "lexical", "hybrid"]

SELECTOR_PROMPTS = {
    "Business v2 (default)": "v3_selector_business_v2.md",
    "Business v1": "v3_selector_business.md",
    "Default": "v3_selector_default.md",
}

SELECTOR_MODELS = ["openweight-large", "openweight-medium", "openweight-small"]

CONFIG_COLORS = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA"]

K_VALUES = [1, 3, 5, 10, 20, 50]

TIMING_KEYS = [
    "query_processing",
    "chunk_retrieval",
    "section_aggregation",
    "llm_selector",
    "context_build",
]

TIMING_LABELS = {
    "query_processing": "Query Processing",
    "chunk_retrieval": "Chunk Retrieval",
    "section_aggregation": "Section Aggregation",
    "llm_selector": "LLM Selector",
    "context_build": "Context Build",
}


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT SAVE / LOAD
# ═══════════════════════════════════════════════════════════════════════════════


def auto_detect_theme(configs) -> str:
    """Auto-detect experiment theme from what varies between configs."""
    if len(configs) < 2:
        return "General"
    base = configs[0]
    diffs = set()
    for cfg in configs[1:]:
        if cfg.embedding_key != base.embedding_key:
            diffs.add("Embeddings")
        if cfg.search_mode != base.search_mode:
            diffs.add("Search_Mode")
        if cfg.enable_hyde != base.enable_hyde:
            diffs.add("HyDE")
        if cfg.enable_acronym_expansion != base.enable_acronym_expansion or cfg.acronym_mode != base.acronym_mode:
            diffs.add("Acronymes")
        if cfg.enable_llm_selector != base.enable_llm_selector:
            diffs.add("LLM_Selector")
        if (cfg.enable_llm_selector or base.enable_llm_selector) and (
            cfg.selector_model != base.selector_model or cfg.selector_prompt != base.selector_prompt
        ):
            diffs.add("LLM_Selector")
        if cfg.enable_chunk_reranker != base.enable_chunk_reranker:
            diffs.add("Chunk_Reranker")
        if cfg.enable_section_reranker != base.enable_section_reranker:
            diffs.add("Section_Reranker")
        if cfg.alpha != base.alpha:
            diffs.add("Search_Mode")
        if set(cfg.extra_de_tables) != set(base.extra_de_tables):
            diffs.add("Sources")
    return " + ".join(sorted(diffs)) if diffs else "General"


def _cfg_to_dict(cfg: "PipelineEvalConfig") -> dict:
    """Serialize a PipelineEvalConfig to JSON-safe dict."""
    return {
        "name": cfg.name,
        "color": cfg.color,
        "embedding_key": cfg.embedding_key,
        "search_mode": cfg.search_mode,
        "alpha": cfg.alpha,
        "enable_acronym_expansion": cfg.enable_acronym_expansion,
        "acronym_mode": cfg.acronym_mode,
        "acronym_llm_model": cfg.acronym_llm_model,
        "enable_hyde": cfg.enable_hyde,
        "hyde_model": cfg.hyde_model,
        "initial_top_k": cfg.initial_top_k,
        "enable_chunk_reranker": cfg.enable_chunk_reranker,
        "rerank_top_k": cfg.rerank_top_k,
        "enable_section_reranker": cfg.enable_section_reranker,
        "section_rerank_top_k": cfg.section_rerank_top_k,
        "enable_llm_selector": cfg.enable_llm_selector,
        "selector_model": cfg.selector_model,
        "selector_prompt": cfg.selector_prompt,
        "ministry": cfg.ministry,
        "extra_de_tables": cfg.extra_de_tables,
    }


def save_experiment(
    results: Dict,
    name: str,
    theme: str,
    description: str = "",
    goldset_names: Optional[List[str]] = None,
    tag_filter: Optional[List[str]] = None,
    publisher_filter: Optional[str] = None,
    elapsed_seconds: float = 0,
):
    """Save experiment results to DB for later review."""
    conn = get_connection()
    cur = conn.cursor()

    configs_json = [_cfg_to_dict(c) for c in results["configs"]]

    # Determine best config by MRR
    best_cfg, best_mrr = None, 0
    for cfg_name, agg in results.get("aggregate", {}).items():
        mrr = agg.get("mrr", 0) or 0
        if mrr > best_mrr:
            best_mrr = mrr
            best_cfg = cfg_name

    # Strip embeddings from per_question to save space
    pq_clean = []
    for q in results.get("per_question", []):
        q_copy = {k: v for k, v in q.items()}
        pq_clean.append(q_copy)

    cur.execute(
        """
        INSERT INTO pipeline_eval_experiments
            (name, theme, description, n_questions, goldset_names, tag_filter,
             publisher_filter, configs, aggregate, per_question,
             best_config, best_mrr, total_time_seconds)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """,
        [
            name,
            theme,
            description,
            results.get("n_questions", 0),
            goldset_names or [],
            tag_filter or [],
            publisher_filter or "Tous",
            json.dumps(configs_json),
            json.dumps(results.get("aggregate", {})),
            json.dumps(pq_clean),
            best_cfg,
            best_mrr,
            elapsed_seconds,
        ],
    )
    exp_id = cur.fetchone()["id"]
    return exp_id


def list_experiments(limit: int = 30) -> List[Dict]:
    """List recent experiments for browsing."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, created_at, name, theme, n_questions, best_config, best_mrr,
               total_time_seconds, goldset_names, tag_filter
        FROM pipeline_eval_experiments
        ORDER BY created_at DESC
        LIMIT %s
    """,
        [limit],
    )
    return cur.fetchall()


def load_experiment(exp_id: int) -> Optional[Dict]:
    """Load a saved experiment by ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM pipeline_eval_experiments WHERE id = %s
    """,
        [exp_id],
    )
    row = cur.fetchone()
    if not row:
        return None

    # Reconstruct configs as PipelineEvalConfig objects
    configs = []
    for cd in row["configs"]:
        cfg = PipelineEvalConfig(
            name=cd["name"],
            color=cd["color"],
            embedding_key=cd.get("embedding_key", "albert_raw"),
            search_mode=cd.get("search_mode", "semantic"),
            alpha=cd.get("alpha", 0.5),
            enable_acronym_expansion=cd.get("enable_acronym_expansion", True),
            acronym_mode=cd.get("acronym_mode", "rule"),
            acronym_llm_model=cd.get("acronym_llm_model", "openweight-medium"),
            enable_hyde=cd.get("enable_hyde", False),
            hyde_model=cd.get("hyde_model", "openweight-medium"),
            initial_top_k=cd.get("initial_top_k", 50),
            enable_chunk_reranker=cd.get("enable_chunk_reranker", False),
            rerank_top_k=cd.get("rerank_top_k", 30),
            enable_section_reranker=cd.get("enable_section_reranker", True),
            section_rerank_top_k=cd.get("section_rerank_top_k", 10),
            enable_llm_selector=cd.get("enable_llm_selector", False),
            selector_model=cd.get("selector_model", "openweight-large"),
            selector_prompt=cd.get("selector_prompt", "v3_selector_business_v2.md"),
            ministry=cd.get("ministry", "matte"),
            extra_de_tables=cd.get("extra_de_tables", []),
        )
        configs.append(cfg)

    return {
        "per_question": row["per_question"] or [],
        "aggregate": row["aggregate"] or {},
        "configs": configs,
        "n_questions": row["n_questions"],
        "metadata": {
            "id": row["id"],
            "name": row["name"],
            "theme": row["theme"],
            "description": row.get("description", ""),
            "created_at": str(row["created_at"]),
            "goldset_names": row.get("goldset_names", []),
            "tag_filter": row.get("tag_filter", []),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

DE_SOURCE_OPTIONS = {
    "RGRH": "rag_chunks_rgrh",
    "DGAFP": "rag_chunks_dgafp",
    "MATTE (DE)": "rag_chunks_matte",
    "Service-Public (DE)": "rag_chunks_service_public",
}


@dataclass
class PipelineEvalConfig:
    """A named pipeline configuration for evaluation."""

    name: str
    color: str

    embedding_key: str = "albert_raw"
    search_mode: str = "semantic"
    alpha: float = 0.5
    enable_acronym_expansion: bool = True
    acronym_mode: str = "rule"  # "rule" (instant, regex-based) or "llm" (LLM-validated, like prod)
    acronym_llm_model: str = "openweight-medium"
    enable_hyde: bool = False
    hyde_model: str = "openweight-medium"
    initial_top_k: int = 50
    enable_chunk_reranker: bool = False
    rerank_top_k: int = 30
    enable_section_reranker: bool = True
    section_rerank_top_k: int = 10
    enable_llm_selector: bool = True
    selector_model: str = "openweight-large"
    selector_prompt: str = "v3_selector_business_v2.md"
    ministry: str = "matte"  # tenant used to render ministry-agnostic prompts
    extra_de_tables: List[str] = field(default_factory=list)


def auto_name(cfg: PipelineEvalConfig) -> str:
    """Generate a descriptive name from config settings."""
    parts = []
    embed_short = {"albert_raw": "Raw", "albert_ctx": "Ctx", "albert_raw_text": "RawTxt", "bge_scaleway": "BGE"}
    parts.append(embed_short.get(cfg.embedding_key, cfg.embedding_key))

    if cfg.search_mode == "hybrid":
        parts.append(f"Hybrid({cfg.alpha:.1f})")
    elif cfg.search_mode == "lexical":
        parts.append("Lexical")
    else:
        parts.append("Semantic")

    if cfg.enable_acronym_expansion:
        if cfg.acronym_mode == "llm":
            parts.append("Acro(LLM)")
        else:
            parts.append("Acro")
    if cfg.enable_hyde:
        model_short = {"openweight-medium": "med", "openweight-large": "lg", "openweight-small": "sm"}
        parts.append(f"HyDE({model_short.get(cfg.hyde_model, cfg.hyde_model)})")
    if cfg.enable_chunk_reranker:
        parts.append(f"ChunkRR({cfg.rerank_top_k})")
    if cfg.enable_section_reranker:
        parts.append(f"SecRR({cfg.section_rerank_top_k})")
    if cfg.enable_llm_selector:
        parts.append(selector_variant_label(cfg.selector_model, cfg.selector_prompt))

    if cfg.extra_de_tables:
        short_names = {"rag_chunks_rgrh": "RGRH", "rag_chunks_dgafp": "DGAFP", "rag_chunks_matte": "DE-MATTE", "rag_chunks_service_public": "DE-SP"}
        de_parts = [short_names.get(t, t) for t in cfg.extra_de_tables]
        parts.append(f"+{'+'.join(de_parts)}")

    return " · ".join(parts)


def render_config_sidebar(idx: int, color: str) -> PipelineEvalConfig:
    """Render sidebar widgets for one pipeline config."""
    default_name = auto_name(PipelineEvalConfig(name="", color=color))
    label = f"Config {idx + 1}" if idx > 0 else "Baseline"

    with st.expander(f"⚙️ {label}", expanded=(idx == 0)):
        col1, col2 = st.columns(2)
        with col1:
            embed_label = st.selectbox(
                "Embedding",
                options=list(EMBEDDING_OPTIONS.keys()),
                key=f"cfg_embed_{idx}",
            )
            search_mode = st.selectbox(
                "Search Mode",
                options=SEARCH_MODE_OPTIONS,
                key=f"cfg_search_{idx}",
            )
        with col2:
            top_k = st.slider("Top K", 10, 100, 50, key=f"cfg_topk_{idx}")
            alpha = 0.5
            if search_mode == "hybrid":
                alpha = st.slider("Alpha (0=lex, 1=sem)", 0.0, 1.0, 0.5, 0.05, key=f"cfg_alpha_{idx}")

        col3, col4 = st.columns(2)
        with col3:
            acronym = st.checkbox("Acronym Expansion", value=True, key=f"cfg_acro_{idx}")
            acro_mode = "rule"
            acro_llm_model = "openweight-medium"
            if acronym:
                acro_mode = st.selectbox(
                    "Mode",
                    ["rule", "llm"],
                    key=f"cfg_acromode_{idx}",
                    help="rule = regex instantané, llm = validation LLM (comme en prod)",
                )
                if acro_mode == "llm":
                    acro_llm_model = st.selectbox(
                        "Acronym LLM",
                        ["openweight-medium", "openweight-large", "openweight-small"],
                        key=f"cfg_acrollm_{idx}",
                    )
            hyde = st.checkbox("HyDE", value=False, key=f"cfg_hyde_{idx}")
            hyde_model = "openweight-medium"
            if hyde:
                hyde_model = st.selectbox(
                    "HyDE Model",
                    ["openweight-medium", "openweight-large", "openweight-small"],
                    key=f"cfg_hydemod_{idx}",
                )
        with col4:
            chunk_rerank = st.checkbox("Chunk Reranker", value=False, key=f"cfg_crerank_{idx}")
            rerank_top_k = 30
            if chunk_rerank:
                rerank_top_k = st.slider("Rerank Top K", 5, 50, 30, key=f"cfg_rtopk_{idx}")

        col5, col6 = st.columns(2)
        with col5:
            sec_rerank = st.checkbox("Section Reranker", value=True, key=f"cfg_srerank_{idx}")
            sec_rerank_top_k = 10
            if sec_rerank:
                sec_rerank_top_k = st.slider("Sec Rerank Top K", 3, 20, 10, key=f"cfg_srtopk_{idx}")
        with col6:
            llm_selector = st.checkbox("LLM Selector", value=(idx == 0), key=f"cfg_llmsel_{idx}")
            sel_model = "openweight-large"
            sel_prompt = "v3_selector_business_v2.md"
            if llm_selector:
                sel_model = st.selectbox("Selector Model", SELECTOR_MODELS, key=f"cfg_selmod_{idx}")
                sel_prompt_label = st.selectbox("Selector Prompt", list(SELECTOR_PROMPTS.keys()), key=f"cfg_selprompt_{idx}")
                sel_prompt = SELECTOR_PROMPTS[sel_prompt_label]
            ministry_id = st.selectbox(
                "Ministère",
                list(MINISTRY_CATALOG.keys()),
                format_func=lambda mid: MINISTRY_CATALOG[mid].label,
                key=f"cfg_ministry_{idx}",
                help=(
                    "Comme en prod : scope le retrieval (table du ministère + service_public + dgafp) ET le rendu des prompts ministère-agnostiques."
                ),
            )

        # Extra DE tables
        extra_de = st.multiselect(
            "📚 Sources supplémentaires (DE tables)",
            options=list(DE_SOURCE_OPTIONS.keys()),
            default=[],
            key=f"cfg_de_{idx}",
            help="Ajouter des tables DE au retrieval V3",
        )
        extra_de_tables = [DE_SOURCE_OPTIONS[k] for k in extra_de]

    cfg = PipelineEvalConfig(
        name="",
        color=color,
        embedding_key=EMBEDDING_OPTIONS[embed_label],
        search_mode=search_mode,
        alpha=alpha,
        enable_acronym_expansion=acronym,
        acronym_mode=acro_mode,
        acronym_llm_model=acro_llm_model,
        enable_hyde=hyde,
        hyde_model=hyde_model,
        initial_top_k=top_k,
        enable_chunk_reranker=chunk_rerank,
        rerank_top_k=rerank_top_k,
        enable_section_reranker=sec_rerank,
        section_rerank_top_k=sec_rerank_top_k,
        enable_llm_selector=llm_selector,
        selector_model=sel_model,
        selector_prompt=sel_prompt,
        ministry=ministry_id,
        extra_de_tables=extra_de_tables,
    )
    cfg.name = auto_name(cfg)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# GOLDSET LOADING
# ═══════════════════════════════════════════════════════════════════════════════


def get_available_goldset_names() -> List[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT goldset_name FROM goldset_questions_v2 WHERE goldset_name IS NOT NULL ORDER BY goldset_name")
    return [r["goldset_name"] for r in cur.fetchall()]


def get_available_tags() -> List[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT unnest(tags) as tag FROM goldset_questions_v2 ORDER BY tag")
    return [r["tag"] for r in cur.fetchall()]


def count_excluded_no_gold(
    goldset_names: Optional[List[str]] = None,
    tag_filter: Optional[List[str]] = None,
) -> int:
    """Count questions that match filters but have no gold_sources (excluded from eval)."""
    conn = get_connection()
    cur = conn.cursor()
    where_clauses = ["(gold_sources IS NULL OR gold_sources = '')"]
    if goldset_names:
        placeholders = ",".join(f"'{g}'" for g in goldset_names)
        where_clauses.append(f"goldset_name IN ({placeholders})")
    if tag_filter:
        tag_array = "ARRAY[" + ",".join(f"'{t}'" for t in tag_filter) + "]"
        where_clauses.append(f"tags @> {tag_array}")
    cur.execute(f"SELECT count(*) as cnt FROM goldset_questions_v2 WHERE {' AND '.join(where_clauses)}")
    return cur.fetchone()["cnt"]


def load_goldset_questions(
    goldset_names: Optional[List[str]] = None,
    tag_filter: Optional[List[str]] = None,
    max_questions: Optional[int] = None,
    publisher_filter: Optional[str] = None,
) -> List[Dict]:
    """Load questions with flexible filtering. Always requires gold_sources for retrieval eval."""
    conn = get_connection()
    cur = conn.cursor()

    # gold_sources required — without it no retrieval metric can be computed
    where_clauses = ["gold_sources IS NOT NULL", "gold_sources != ''"]

    if goldset_names:
        placeholders = ",".join(f"'{g}'" for g in goldset_names)
        where_clauses.append(f"goldset_name IN ({placeholders})")

    if tag_filter:
        tag_array = "ARRAY[" + ",".join(f"'{t}'" for t in tag_filter) + "]"
        where_clauses.append(f"tags @> {tag_array}")

    where_sql = " AND ".join(where_clauses)
    # The publisher filter is applied in Python (gold_sources can be multi-valued),
    # so the SQL LIMIT must be deferred until after that filter — capping rows
    # before narrowing to the publisher would return far fewer (or zero) questions
    # than requested and miss matches beyond the limit.
    apply_publisher_filter = bool(publisher_filter and publisher_filter != "Tous")
    limit_sql = f"LIMIT {max_questions}" if (max_questions and not apply_publisher_filter) else ""

    cur.execute(f"""
        SELECT id, question, gold_answer, gold_sources, theme, tags, difficulty,
               goldset_name
        FROM goldset_questions_v2
        WHERE {where_sql}
        ORDER BY id
        {limit_sql}
    """)
    rows = cur.fetchall()
    if apply_publisher_filter:
        cur.execute("SELECT DISTINCT short_id FROM rag_documents WHERE publisher = %s", (publisher_filter,))
        pub_keys = {str(row["short_id"]).strip().lower() for row in cur.fetchall() if str(row["short_id"] or "").strip()}
        rows = [row for row in rows if pub_keys & {source.lower() for source in parse_gold_sources(row.get("gold_sources"))}]
        if max_questions:
            rows = rows[:max_questions]
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# LIGHTWEIGHT EVAL PIPELINE
# Uses V3 modules directly — no intent gating, no legal refs, no escalation.
# This page scores the selector's first pass; the #298 Suivi-Tests replay is the
# production-parity path for validating selector_retry.
# ~10x faster than run_retrieval_only()
# ═══════════════════════════════════════════════════════════════════════════════


@st.cache_resource
def get_acronym_dict() -> Dict:
    """Load acronym dictionary once."""
    try:
        return _get_acro()
    except Exception:
        return {}


def run_lightweight_pipeline(
    query: str,
    cfg: PipelineEvalConfig,
    _cached_modules: Optional[Dict] = None,
) -> Dict:
    """
    Run retrieval+aggregation via rag_v3_clean.Pipeline for evaluation.

    Returns dict with section/chunk results, timing, and source distributions.
    """
    timing: Dict[str, float] = {}

    search_map = {"semantic": SearchMode.SEMANTIC, "lexical": SearchMode.LEXICAL, "hybrid": SearchMode.HYBRID}
    embed_map = {
        "albert_raw": EmbeddingModel.ALBERT,
        "albert_ctx": EmbeddingModel.ALBERT,
        "albert_raw_text": EmbeddingModel.ALBERT,
        "bge_scaleway": EmbeddingModel.BGE_SCALEWAY,
    }

    config = RAGConfigV3()
    config.retrieval = RetrievalConfig(
        search_mode=search_map.get(cfg.search_mode, SearchMode.SEMANTIC),
        embedding_model=embed_map.get(cfg.embedding_key, EmbeddingModel.ALBERT),
        initial_top_k=cfg.initial_top_k,
        alpha=cfg.alpha,
        enable_chunk_reranker=cfg.enable_chunk_reranker,
        chunk_rerank_top_k=cfg.rerank_top_k,
    )
    config.aggregation = SectionAggregationConfig(
        enable_section_reranker=cfg.enable_section_reranker,
        section_rerank_top_k=cfg.section_rerank_top_k,
    )
    config.query_processor = QueryProcessorConfig(
        enable_acronym_expansion=cfg.enable_acronym_expansion,
        enable_intent_gating=False,
        enable_hyde=cfg.enable_hyde,
    )
    config.selector = SelectorConfig(
        enabled=cfg.enable_llm_selector,
        model=getattr(cfg, "selector_model", "openweight-large"),
        prompt_name=getattr(cfg, "selector_prompt", "v3_selector_business_v2.md"),
    )
    config.verbose = False

    pipe = create_pipeline(config, dsn=DSN)

    t0 = time.time()
    qr = pipe.process_query(query)
    timing["query_processing"] = time.time() - t0

    query_for_retrieval = qr.query_for_retrieval

    # Scope retrieval to the selected ministry like prod (ministry chunk table +
    # shared service_public/dgafp), so the selector prompt rendered for that
    # ministry ranks over coherent sources instead of the full default table set.
    scope = build_retrieval_scope(cfg.ministry)
    force_hybrid = {"dgafp"} if "dgafp" in scope.table_keys else set()

    t0 = time.time()
    chunks = pipe._retriever.retrieve(
        query_for_retrieval,
        tables=list(scope.table_keys),
        force_hybrid_tables=force_hybrid,
    )
    timing["chunk_retrieval"] = time.time() - t0

    t0 = time.time()
    sections = pipe._aggregator.aggregate(chunks, query=query_for_retrieval)
    timing["section_aggregation"] = time.time() - t0

    # LLM Selector BEFORE context builder (same order as prod pipeline)
    selected_sections = sections
    selector_detail = None
    if cfg.enable_llm_selector and sections:
        t0 = time.time()
        try:
            # A bare Pipeline never populates ``pipe._selector`` (only run()/run_stream
            # do), so build a request-scoped selector here and render its prompt for
            # the configured ministry.
            selector = ContextSelector(config.selector)
            ministry = resolve_ministry(cfg.ministry)
            selected_sections = selector.select(query_for_retrieval, sections, ministry=ministry)
            selector_detail = {
                "n_items_input": len(sections),
                "n_items_selected": len(selected_sections),
                "all_rejected": selector.all_rejected,
                "reason": selector.last_reasoning,
            }
        except Exception as e:
            selector_detail = {"error": str(e)}
        timing["llm_selector"] = time.time() - t0

    t0 = time.time()
    selected_items = pipe._context_builder.build(selected_sections)
    timing["context_build"] = time.time() - t0

    def _publisher_dist(items, attr="publisher"):
        dist = Counter()
        for it in items:
            pub = it.get(attr, "unknown") if isinstance(it, dict) else getattr(it, attr, "unknown")
            dist[pub or "unknown"] += 1
        return dict(dist)

    source_dist_chunks = _publisher_dist([{"publisher": c.metadata.get("publisher", c.table_source)} for c in chunks])
    source_dist_sections = _publisher_dist([{"publisher": s.publisher} for s in sections])
    source_dist_selected = _publisher_dist([{"publisher": s.publisher} for s in selected_sections])

    def _sections_to_dicts(secs):
        return [
            {
                "doc_short_id": s.metadata.get("doc_short_id") or "",
                "doc_title": s.heading,
                "doc_publisher": s.publisher or "",
                "section_heading": s.heading,
                "aggregated_score": s.score,
                "final_score": s.score,
                "chunk_count": len(s.chunks),
                "token_count": s.token_estimate,
            }
            for s in secs
        ]

    section_results = _sections_to_dicts(sections)
    selected_results = _sections_to_dicts(selected_sections)

    chunk_results = [
        {
            "doc_short_id": c.metadata.get("source_document_id", ""),
            "chunk_id": c.chunk_id,
            "section_heading": c.metadata.get("heading", ""),
            "final_score": c.score,
            "doc_publisher": c.metadata.get("publisher", c.table_source),
        }
        for c in chunks
    ]

    context_token_count = sum(it.token_estimate for it in selected_items)

    return {
        "sections": section_results,
        "selected_sections": selected_results,
        "chunks": chunk_results,
        "timing": timing,
        "query_processed": query_for_retrieval,
        "hyde_doc": None,
        "expanded_acronyms": qr.expanded_acronyms,
        "n_chunks": len(chunks),
        "n_sections": len(sections),
        "n_selected": len(selected_sections),
        "context_token_count": context_token_count,
        "selector_detail": selector_detail,
        "source_dist": {
            "after_chunks": source_dist_chunks,
            "after_sections": source_dist_sections,
            "after_selector": source_dist_selected,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════


def _is_hit(item: Dict, gold_sources: str) -> bool:
    short_id = item.get("doc_short_id", "")
    return gold_sources_match(gold_sources, short_id)


def compute_recall_at_k(items: List[Dict], gold: str, k: int) -> int:
    return 1 if any(_is_hit(it, gold) for it in items[:k]) else 0


def compute_mrr(items: List[Dict], gold: str, max_k: int = 100) -> float:
    for i, it in enumerate(items[:max_k]):
        if _is_hit(it, gold):
            return 1.0 / (i + 1)
    return 0.0


def compute_ndcg_at_k(items: List[Dict], gold: str, k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, it in enumerate(items[:k]) if _is_hit(it, gold))
    n_rel = sum(1 for it in items[:k] if _is_hit(it, gold))
    if n_rel == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(j + 2) for j in range(n_rel))
    return dcg / idcg if idcg > 0 else 0.0


def compute_first_hit_rank(items: List[Dict], gold: str) -> Optional[int]:
    for i, it in enumerate(items):
        if _is_hit(it, gold):
            return i + 1
    return None


def compute_all_metrics(items: List[Dict], gold: str) -> Dict:
    m = {}
    for k in K_VALUES:
        m[f"recall@{k}"] = compute_recall_at_k(items, gold, k)
        m[f"ndcg@{k}"] = compute_ndcg_at_k(items, gold, k)
    m["mrr"] = compute_mrr(items, gold)
    m["first_hit_rank"] = compute_first_hit_rank(items, gold)
    return m


# ═══════════════════════════════════════════════════════════════════════════════
# EVALUATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════


def run_evaluation(
    configs: List[PipelineEvalConfig],
    goldset_names: Optional[List[str]] = None,
    tag_filter: Optional[List[str]] = None,
    max_questions: Optional[int] = None,
    publisher_filter: Optional[str] = None,
    progress_callback=None,
) -> Dict:
    questions = load_goldset_questions(goldset_names, tag_filter, max_questions, publisher_filter)
    if not questions:
        return {"per_question": [], "aggregate": {}, "configs": configs, "n_questions": 0}

    total = len(questions)
    per_question = []
    config_metrics = {cfg.name: [] for cfg in configs}
    config_timings = {cfg.name: [] for cfg in configs}

    # Shared module cache across questions (reuse retriever/aggregator objects)
    cached_modules: Dict = {}

    for q_idx, q in enumerate(questions):
        if progress_callback:
            progress_callback((q_idx + 1) / total, f"Q {q_idx + 1}/{total} — {q['question'][:50]}...")

        gold = q.get("gold_sources") or ""
        tags = q.get("tags") or []

        q_result = {
            "question_id": q["id"],
            "question": q["question"],
            "gold_sources": format_gold_sources(gold),
            "theme": q.get("theme", ""),
            "tags": tags,
            "goldset_name": q.get("goldset_name", ""),
        }

        for cfg in configs:
            try:
                result = run_lightweight_pipeline(q["question"], cfg, cached_modules)

                # Metrics on final sections (post-selector if enabled)
                final_items = final_items_for_metrics(result)
                metrics = compute_all_metrics(final_items, gold)
                timing = result["timing"]

                q_result[cfg.name] = {
                    "metrics": metrics,
                    "timing": timing,
                    "n_chunks": result["n_chunks"],
                    "n_sections": result["n_sections"],
                    "n_selected": result["n_selected"],
                    "context_token_count": result.get("context_token_count", 0),
                    "query_processed": result["query_processed"],
                    "hyde_doc": result.get("hyde_doc"),
                    "expanded_acronyms": result.get("expanded_acronyms", []),
                    "selector_detail": result.get("selector_detail"),
                    "source_dist": result.get("source_dist", {}),
                    "skipped": False,
                }
                config_metrics[cfg.name].append(metrics)
                config_timings[cfg.name].append(timing)

            except Exception as e:
                import traceback

                print(f"  ❌ [ERROR] Config={cfg.name}, Q={q['question'][:40]}: {e}")
                traceback.print_exc()
                q_result[cfg.name] = {
                    "metrics": {},
                    "timing": {},
                    "error": str(e),
                    "skipped": True,
                }
                config_metrics[cfg.name].append({})
                config_timings[cfg.name].append({})

        per_question.append(q_result)

    # Aggregate
    aggregate = {}
    for cfg in configs:
        valid_m = [m for m in config_metrics[cfg.name] if m]
        valid_t = [t for t in config_timings[cfg.name] if t]
        n_errors = len(config_metrics[cfg.name]) - len(valid_m)
        n = len(valid_m)
        agg = {"n_questions": n, "n_errors": n_errors}
        if n_errors > 0:
            print(f"  ⚠️ [AGGREGATE] {cfg.name}: {n_errors} errors out of {total} questions")

        if n > 0:
            for k in K_VALUES:
                agg[f"recall@{k}"] = sum(m.get(f"recall@{k}", 0) for m in valid_m) / n
                agg[f"ndcg@{k}"] = sum(m.get(f"ndcg@{k}", 0) for m in valid_m) / n
            agg["mrr"] = sum(m.get("mrr", 0) for m in valid_m) / n
            ranks = [m["first_hit_rank"] for m in valid_m if m.get("first_hit_rank")]
            agg["avg_first_hit_rank"] = sum(ranks) / len(ranks) if ranks else None
            agg["n_found"] = len(ranks)
            agg["n_missed"] = n - len(ranks)

        if valid_t:
            for key in TIMING_KEYS:
                vals = [t.get(key, 0) for t in valid_t if key in t]
                agg[f"avg_timing_{key}"] = (sum(vals) / len(vals)) * 1000 if vals else 0
            totals = [sum(t.get(k, 0) for k in TIMING_KEYS if k in t) * 1000 for t in valid_t]
            agg["avg_total_time_ms"] = sum(totals) / len(totals) if totals else 0

        # Average context token count
        ctx_tokens = []
        for q in per_question:
            qdata = q.get(cfg.name)
            if qdata and not qdata.get("skipped") and qdata.get("context_token_count"):
                ctx_tokens.append(qdata["context_token_count"])
        agg["avg_context_tokens"] = int(sum(ctx_tokens) / len(ctx_tokens)) if ctx_tokens else 0

        aggregate[cfg.name] = agg

    return {
        "per_question": per_question,
        "aggregate": aggregate,
        "configs": configs,
        "n_questions": total,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════════


def format_pct(val) -> str:
    return f"{val * 100:.1f}%" if val is not None else "—"


def build_summary_table(results: Dict) -> pd.DataFrame:
    rows = []
    for cfg in results["configs"]:
        agg = results["aggregate"].get(cfg.name, {})
        if not agg:
            continue
        row = {"Config": cfg.name}
        for k in K_VALUES:
            row[f"Recall@{k}"] = format_pct(agg.get(f"recall@{k}"))
        row["MRR"] = f"{agg.get('mrr', 0):.4f}"
        for k in [5, 10]:
            row[f"NDCG@{k}"] = f"{agg.get(f'ndcg@{k}', 0):.4f}"
        avg_rank = agg.get("avg_first_hit_rank")
        row["Avg Rank"] = f"{avg_rank:.2f}" if avg_rank else "—"
        n_err = agg.get("n_errors", 0)
        found_missed = f"{agg.get('n_found', 0)} / {agg.get('n_missed', 0)}"
        if n_err > 0:
            found_missed += f" ({n_err} err)"
        row["Found / Missed"] = found_missed
        ctx_tok = agg.get("avg_context_tokens", 0)
        row["Ctx Tokens"] = f"{ctx_tok:,}" if ctx_tok else "—"
        row["Latence (ms)"] = f"{agg.get('avg_total_time_ms', 0):.0f}"
        rows.append(row)
    return pd.DataFrame(rows)


def recall_curve_chart(results: Dict) -> go.Figure:
    fig = go.Figure()
    for cfg in results["configs"]:
        agg = results["aggregate"].get(cfg.name, {})
        if not agg:
            continue
        ys = [agg.get(f"recall@{k}", 0) * 100 for k in K_VALUES]
        fig.add_trace(
            go.Scatter(
                x=K_VALUES,
                y=ys,
                mode="lines+markers",
                name=cfg.name,
                line=dict(color=cfg.color, width=3),
                marker=dict(size=10),
            )
        )
    fig.update_layout(
        title="Recall@K",
        xaxis_title="K",
        yaxis_title="Recall (%)",
        yaxis=dict(range=[0, 105]),
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def mrr_bar_chart(results: Dict) -> go.Figure:
    names, values, colors = [], [], []
    for cfg in results["configs"]:
        agg = results["aggregate"].get(cfg.name, {})
        if not agg:
            continue
        names.append(cfg.name)
        values.append(agg.get("mrr", 0))
        colors.append(cfg.color)
    fig = go.Figure(
        data=[
            go.Bar(
                x=names,
                y=values,
                marker_color=colors,
                text=[f"{v:.4f}" for v in values],
                textposition="auto",
            )
        ]
    )
    fig.update_layout(title="MRR", yaxis_title="MRR", yaxis=dict(range=[0, 1.05]), height=350)
    return fig


def first_hit_histogram(results: Dict) -> go.Figure:
    fig = go.Figure()
    for cfg in results["configs"]:
        ranks = []
        for q in results.get("per_question", []):
            data = q.get(cfg.name)
            if data and not data.get("skipped"):
                r = data["metrics"].get("first_hit_rank")
                if r is not None:
                    ranks.append(r)
        if ranks:
            fig.add_trace(
                go.Histogram(
                    x=ranks,
                    name=cfg.name,
                    marker_color=cfg.color,
                    opacity=0.7,
                    nbinsx=20,
                )
            )
    fig.update_layout(
        title="Distribution du rang du 1er hit",
        xaxis_title="Rang",
        yaxis_title="Nb questions",
        barmode="overlay",
        height=400,
    )
    return fig


def latency_stacked_bar(results: Dict) -> go.Figure:
    fig = go.Figure()
    config_names = [cfg.name for cfg in results["configs"] if cfg.name in results["aggregate"]]
    module_colors = {
        "query_processing": "#636EFA",
        "chunk_retrieval": "#00CC96",
        "section_aggregation": "#AB63FA",
        "llm_selector": "#EF553B",
        "context_build": "#FFA15A",
    }
    for key in TIMING_KEYS:
        vals = []
        for cfg in results["configs"]:
            agg = results["aggregate"].get(cfg.name, {})
            vals.append(agg.get(f"avg_timing_{key}", 0))
        if any(v > 0 for v in vals):
            fig.add_trace(
                go.Bar(
                    name=TIMING_LABELS.get(key, key),
                    x=config_names,
                    y=vals,
                    marker_color=module_colors.get(key, "#999"),
                )
            )
    fig.update_layout(
        barmode="stack",
        title="Latence moyenne par module (ms)",
        yaxis_title="Temps (ms)",
        height=450,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def latency_detail_table(results: Dict) -> pd.DataFrame:
    rows = []
    for cfg in results["configs"]:
        agg = results["aggregate"].get(cfg.name, {})
        if not agg:
            continue
        row = {"Config": cfg.name}
        total = 0
        for key in TIMING_KEYS:
            val = agg.get(f"avg_timing_{key}", 0)
            row[TIMING_LABELS.get(key, key)] = f"{val:.0f}" if val > 0 else "—"
            total += val
        row["Total (ms)"] = f"{total:.0f}"
        rows.append(row)
    return pd.DataFrame(rows)


def _build_aliases(configs) -> Dict[str, str]:
    """Build short aliases: Config A, Config B, ... for display."""
    letters = "ABCD"
    return {cfg.name: letters[i] if i < len(letters) else f"C{i + 1}" for i, cfg in enumerate(configs)}


def per_question_df(results: Dict) -> pd.DataFrame:
    rows = []
    configs = [c for c in results["configs"] if c.name in results["aggregate"]]
    aliases = _build_aliases(configs)

    for q in results.get("per_question", []):
        row = {
            "ID": q["question_id"],
            "Question": q["question"][:80],
            "Gold": q.get("gold_sources", ""),
            "Tags": ", ".join(t for t in q.get("tags", []) if t != "common_corpus"),
        }

        any_query_modified = False
        for cfg in configs:
            a = aliases[cfg.name]
            data = q.get(cfg.name)
            if data and not data.get("skipped"):
                m = data["metrics"]
                row[f"{a} hit@10"] = "✅" if m.get("recall@10", 0) == 1 else "❌"
                rank = m.get("first_hit_rank")
                row[f"{a} rank"] = rank if rank else "—"
                row[f"{a} MRR"] = f"{m.get('mrr', 0):.3f}"
                q_proc = data.get("query_processed", "")
                hyde = data.get("hyde_doc")
                acros = data.get("expanded_acronyms", [])
                modified = bool(q_proc and q_proc != q["question"]) or bool(hyde)
                if hyde:
                    row[f"{a} query"] = f"[HyDE] {hyde[:100]}"
                    any_query_modified = True
                elif q_proc and q_proc != q["question"]:
                    prefix = f"[Acro: {', '.join(acros)}] " if acros else ""
                    row[f"{a} query"] = f"{prefix}{q_proc[:120]}"
                    any_query_modified = True
                else:
                    row[f"{a} query"] = ""
            else:
                row[f"{a} hit@10"] = "⬜"
                row[f"{a} rank"] = "—"
                row[f"{a} MRR"] = "—"
                row[f"{a} query"] = ""

        row["Query modifiée"] = "✅" if any_query_modified else ""

        # Outcome
        hits = {}
        for cfg in configs:
            a = aliases[cfg.name]
            data = q.get(cfg.name)
            if data and not data.get("skipped"):
                hits[a] = data["metrics"].get("recall@10", 0) == 1
        n_hits = sum(hits.values())
        if n_hits == len(hits) and len(hits) > 0:
            row["Outcome"] = "All hit"
        elif n_hits == 0:
            row["Outcome"] = "All miss"
        else:
            row["Outcome"] = ", ".join(n for n, h in hits.items() if h)
        rows.append(row)

    return pd.DataFrame(rows)


def ablation_matrix(results: Dict) -> pd.DataFrame:
    configs = results["configs"]
    if len(configs) < 2:
        return pd.DataFrame()
    baseline_agg = results["aggregate"].get(configs[0].name, {})
    rows = []
    for cfg in configs[1:]:
        agg = results["aggregate"].get(cfg.name, {})
        row = {"Config": f"{cfg.name} vs {configs[0].name}"}
        for metric in ["recall@5", "recall@10", "mrr", "ndcg@10"]:
            delta = (agg.get(metric, 0) or 0) - (baseline_agg.get(metric, 0) or 0)
            sign = "+" if delta > 0 else ""
            row[f"Δ {metric}"] = f"{sign}{delta * 100:.1f}pp" if "recall" in metric else f"{sign}{delta:.4f}"
        base_rank = baseline_agg.get("avg_first_hit_rank")
        cfg_rank = agg.get("avg_first_hit_rank")
        if base_rank and cfg_rank:
            d = cfg_rank - base_rank
            row["Δ Rank"] = f"{'+' if d > 0 else ''}{d:.2f}"
        else:
            row["Δ Rank"] = "—"
        d_time = (agg.get("avg_total_time_ms", 0) or 0) - (baseline_agg.get("avg_total_time_ms", 0) or 0)
        row["Δ Latence"] = f"{'+' if d_time > 0 else ''}{d_time:.0f}ms"

        # Module differences
        diffs = []
        b = configs[0]
        if cfg.enable_hyde != b.enable_hyde:
            diffs.append(f"HyDE={'ON' if cfg.enable_hyde else 'OFF'}")
        if cfg.enable_chunk_reranker != b.enable_chunk_reranker:
            diffs.append(f"ChunkRR={'ON' if cfg.enable_chunk_reranker else 'OFF'}")
        if cfg.enable_section_reranker != b.enable_section_reranker:
            diffs.append(f"SecRR={'ON' if cfg.enable_section_reranker else 'OFF'}")
        if cfg.enable_llm_selector != b.enable_llm_selector:
            diffs.append(f"Selector={'ON' if cfg.enable_llm_selector else 'OFF'}")
        if cfg.enable_acronym_expansion != b.enable_acronym_expansion:
            diffs.append(f"Acro={'ON' if cfg.enable_acronym_expansion else 'OFF'}")
        if cfg.embedding_key != b.embedding_key:
            diffs.append(f"Embed={cfg.embedding_key}")
        if cfg.search_mode != b.search_mode:
            diffs.append(f"Search={cfg.search_mode}")
        if set(cfg.extra_de_tables) != set(b.extra_de_tables):
            added = set(cfg.extra_de_tables) - set(b.extra_de_tables)
            short = {"rag_chunks_rgrh": "RGRH", "rag_chunks_dgafp": "DGAFP", "rag_chunks_matte": "DE-MATTE", "rag_chunks_service_public": "DE-SP"}
            diffs.append(f"+{','.join(short.get(t, t) for t in added)}" if added else "DE changed")
        row["Modules modifiés"] = " | ".join(diffs) if diffs else "Identique"
        rows.append(row)
    return pd.DataFrame(rows)


def ablation_heatmap(results: Dict) -> Optional[go.Figure]:
    configs = results["configs"]
    if len(configs) < 2:
        return None
    baseline_agg = results["aggregate"].get(configs[0].name, {})
    metrics = ["recall@5", "recall@10", "mrr", "ndcg@10"]
    labels = ["Recall@5", "Recall@10", "MRR", "NDCG@10"]
    z, text_vals = [], []
    for cfg in configs[1:]:
        agg = results["aggregate"].get(cfg.name, {})
        row_z, row_t = [], []
        for m in metrics:
            d = (agg.get(m, 0) or 0) - (baseline_agg.get(m, 0) or 0)
            row_z.append(d)
            s = "+" if d > 0 else ""
            row_t.append(f"{s}{d * 100:.1f}pp" if "recall" in m else f"{s}{d:.4f}")
        z.append(row_z)
        text_vals.append(row_t)

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=labels,
            y=[c.name for c in configs[1:]],
            text=text_vals,
            texttemplate="%{text}",
            colorscale="RdYlGn",
            zmid=0,
        )
    )
    fig.update_layout(title=f"Ablation vs {configs[0].name}", height=max(200, 80 * len(configs[1:])))
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN UI
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.subheader("📋 Goldset")

    goldset_names = get_available_goldset_names()
    available_tags = get_available_tags()

    selected_goldsets = st.multiselect(
        "Goldset(s)",
        options=goldset_names,
        help="Filtrer par goldset_name. Vide = tous.",
    )
    selected_goldsets = selected_goldsets if selected_goldsets else None

    tag_filter = st.multiselect(
        "Tags",
        options=available_tags,
        help="Filtrer par tags (AND). Ex: 'acronym' pour tester l'expansion.",
    )
    tag_filter = tag_filter if tag_filter else None

    publisher_filter = st.selectbox("Source", ["Tous", "MATTE", "Service-Public"])
    max_questions = st.number_input("Max questions", min_value=0, value=0, step=10, help="0 = toutes")
    max_questions = max_questions if max_questions > 0 else None

    st.divider()
    st.subheader("⚙️ Configurations")
    n_configs = st.slider("Nombre de configs", 2, 4, 2)

    configs = []
    for i in range(n_configs):
        cfg = render_config_sidebar(i, CONFIG_COLORS[i])
        configs.append(cfg)

    st.divider()
    st.subheader("📊 Résumé")
    for cfg in configs:
        st.caption(f"**{cfg.name}**")

    # ── Load saved experiment ──
    st.divider()
    st.subheader("💾 Expériences")
    try:
        experiments = list_experiments(20)
    except Exception:
        experiments = []

    if experiments:
        exp_options = {
            f"#{e['id']} — {e['theme']} — {e['name']} ({e['n_questions']}q, MRR={(e.get('best_mrr') or 0):.3f})": e["id"] for e in experiments
        }
        selected_exp = st.selectbox("Charger une expérience", ["—"] + list(exp_options.keys()))
        if selected_exp != "—":
            col_load, col_del = st.columns(2)
            with col_load:
                if st.button("📂 Charger", key="load_exp"):
                    exp_id = exp_options[selected_exp]
                    loaded = load_experiment(exp_id)
                    if loaded:
                        st.session_state["pipe_eval_results"] = loaded
                        st.session_state["loaded_experiment"] = loaded.get("metadata", {})
                        st.rerun()
                    else:
                        st.error("Impossible de charger cette expérience")
            with col_del:
                if st.button("🗑️ Supprimer", key="del_exp"):
                    exp_id = exp_options[selected_exp]
                    try:
                        conn = get_connection()
                        conn.cursor().execute("DELETE FROM pipeline_eval_experiments WHERE id = %s", [exp_id])
                        st.success(f"Expérience #{exp_id} supprimée")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erreur suppression : {e}")
    else:
        st.caption("Aucune expérience sauvegardée")


# ── Tabs ──
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "🚀 Run & Overview",
        "⏱️ Latence",
        "📋 Détail par question",
        "🔬 Ablation",
        "📊 Sources & Selector",
    ]
)

with tab1:
    st.header("🚀 Run & Overview")

    # Preview
    preview_qs = load_goldset_questions(
        selected_goldsets, tag_filter, max_questions=5, publisher_filter=publisher_filter if publisher_filter != "Tous" else None
    )
    n_total = len(load_goldset_questions(selected_goldsets, tag_filter, publisher_filter=publisher_filter if publisher_filter != "Tous" else None))

    n_excluded = count_excluded_no_gold(selected_goldsets, tag_filter)

    filters = []
    if selected_goldsets:
        filters.append(f"goldsets: {', '.join(selected_goldsets)}")
    if tag_filter:
        filters.append(f"tags: {', '.join(tag_filter)}")
    if publisher_filter != "Tous":
        filters.append(f"source: {publisher_filter}")
    filter_str = f" ({' | '.join(filters)})" if filters else ""

    excluded_str = f" — _{n_excluded} exclues (pas de gold_source)_" if n_excluded > 0 else ""
    st.info(f"**{n_total} questions** avec gold_source{filter_str}{excluded_str}")

    if preview_qs:
        with st.expander("Aperçu", expanded=False):
            for q in preview_qs[:5]:
                tags = ", ".join(t for t in (q.get("tags") or []) if t != "common_corpus")
                st.markdown(f"- {q['question'][:100]} — `{q.get('gold_sources', '')}` ({tags})")

    run_btn = st.button("▶️ Lancer l'évaluation", type="primary", width="stretch")

    if run_btn:
        progress_bar = st.progress(0, text="Initialisation...")
        t_start = time.time()

        with st.spinner("Évaluation en cours..."):
            results = run_evaluation(
                configs=configs,
                goldset_names=selected_goldsets,
                tag_filter=tag_filter,
                max_questions=max_questions,
                publisher_filter=publisher_filter if publisher_filter != "Tous" else None,
                progress_callback=lambda p, t: progress_bar.progress(p, text=t),
            )
        elapsed = time.time() - t_start
        progress_bar.empty()
        results["configs"] = configs
        st.session_state["pipe_eval_results"] = results
        st.session_state["pipe_eval_elapsed"] = elapsed
        st.session_state.pop("loaded_experiment", None)
        st.success(f"Terminé : {results['n_questions']} questions × {len(configs)} configs en {elapsed:.0f}s")

        # ── Debug diagnostic ──
        with st.expander("🔍 Debug diagnostic", expanded=False):
            st.markdown("##### 📊 Per-config diagnostic")
            for cfg in configs:
                agg = results.get("aggregate", {}).get(cfg.name, {})
                n_err = agg.get("n_errors", 0)
                st.markdown(f"**{cfg.name}** (`{cfg.search_mode}`) — n_questions={agg.get('n_questions', 0)}, n_errors={n_err}")

                pq = results.get("per_question", [])
                if pq:
                    first_q = pq[0]
                    q_data = first_q.get(cfg.name, {})
                    if q_data.get("skipped"):
                        st.error(f"  ❌ Q1 error: {q_data.get('error', 'unknown')}")
                    else:
                        st.markdown(
                            f"  Q1: n_chunks={q_data.get('n_chunks', '?')}, "
                            f"n_sections={q_data.get('n_sections', '?')}, "
                            f"n_selected={q_data.get('n_selected', '?')}"
                        )
                        metrics = q_data.get("metrics", {})
                        mrr_val = metrics.get("mrr")
                        if isinstance(mrr_val, (int, float)):
                            st.markdown(f"  Q1 metrics: recall@10={metrics.get('recall@10', '?')}, mrr={mrr_val:.4f}")
                        else:
                            st.markdown(f"  Q1 metrics: {metrics}")
                        gold = first_q.get("gold_sources", "")
                        st.markdown(f"  Q1 gold_source: `{gold}`")

    results = st.session_state.get("pipe_eval_results")
    if results and results.get("aggregate"):
        # ── Save experiment ──
        loaded_meta = st.session_state.get("loaded_experiment")
        if loaded_meta:
            st.info(
                f"🔄 Expérience chargée : **{loaded_meta.get('name', '')}** "
                f"(#{loaded_meta.get('id')}, {loaded_meta.get('theme')}, {loaded_meta.get('created_at', '')[:16]})"
            )
        else:
            detected_theme = auto_detect_theme(results.get("configs", configs))
            with st.expander("💾 Sauvegarder cette expérience", expanded=False):
                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    exp_theme = st.text_input("Thème", value=detected_theme, key="save_theme")
                with col_s2:
                    elapsed_s = st.session_state.get("pipe_eval_elapsed", 0)
                    default_name = f"{exp_theme} — {results['n_questions']}q — {elapsed_s:.0f}s"
                    exp_name = st.text_input("Nom", value=default_name, key="save_name")
                exp_desc = st.text_input("Notes (optionnel)", key="save_desc")
                if st.button("💾 Enregistrer", key="save_exp_btn"):
                    try:
                        exp_id = save_experiment(
                            results=results,
                            name=exp_name,
                            theme=exp_theme,
                            description=exp_desc,
                            goldset_names=selected_goldsets,
                            tag_filter=tag_filter,
                            publisher_filter=publisher_filter,
                            elapsed_seconds=elapsed_s,
                        )
                        st.success(f"✅ Expérience #{exp_id} enregistrée !")
                    except Exception as e:
                        st.error(f"Erreur sauvegarde : {e}")

        st.subheader("📊 Résultats")
        st.dataframe(build_summary_table(results), width="stretch", hide_index=True)

        col1, col2 = st.columns(2)
        with col1:
            st.plotly_chart(recall_curve_chart(results), width="stretch")
        with col2:
            st.plotly_chart(mrr_bar_chart(results), width="stretch")
        st.plotly_chart(first_hit_histogram(results), width="stretch")


with tab2:
    st.header("⏱️ Latence par module")
    results = st.session_state.get("pipe_eval_results")
    if not results or not results.get("aggregate"):
        st.info("Lancez d'abord une évaluation.")
    else:
        st.plotly_chart(latency_stacked_bar(results), width="stretch")
        st.subheader("Détail par module")
        st.dataframe(latency_detail_table(results), width="stretch", hide_index=True)

        # Boxplot
        box_data = []
        for q in results.get("per_question", []):
            for cfg in results["configs"]:
                data = q.get(cfg.name)
                if data and not data.get("skipped"):
                    t = data.get("timing", {})
                    total_ms = sum(t.get(k, 0) for k in TIMING_KEYS if k in t) * 1000
                    box_data.append({"Config": cfg.name, "Latence (ms)": total_ms})
        if box_data:
            fig = px.box(
                pd.DataFrame(box_data),
                x="Config",
                y="Latence (ms)",
                color="Config",
                color_discrete_map={c.name: c.color for c in results["configs"]},
                title="Distribution latence par question",
            )
            fig.update_layout(height=400, showlegend=False)
            st.plotly_chart(fig, width="stretch")


with tab3:
    st.header("📋 Détail par question")
    results = st.session_state.get("pipe_eval_results")
    if not results or not results.get("aggregate"):
        st.info("Lancez d'abord une évaluation.")
    else:
        pq_df = per_question_df(results)

        # Legend: show alias → full config name mapping
        configs_for_detail = [c for c in results["configs"] if c.name in results["aggregate"]]
        aliases = _build_aliases(configs_for_detail)
        legend_parts = [f"**{a}** = {name}" for name, a in aliases.items()]
        st.caption(" · ".join(legend_parts))

        # ── Filters row ──
        fcol1, fcol2, fcol3 = st.columns(3)

        with fcol1:
            # Outcome filter
            outcomes_available = sorted(pq_df["Outcome"].unique()) if "Outcome" in pq_df.columns else []
            outcome_filter = st.multiselect(
                "Filtrer par Outcome",
                options=outcomes_available,
                help="All hit, All miss, ou configs gagnantes",
            )

        with fcol2:
            # Tag filter
            tags_in_results = set()
            for q in results.get("per_question", []):
                for t in q.get("tags", []):
                    if t != "common_corpus":
                        tags_in_results.add(t)
            detail_tags = st.multiselect("Filtrer par tags", sorted(tags_in_results))

        with fcol3:
            # Query modified filter
            query_mod_filter = st.checkbox(
                "Query modifiée uniquement",
                value=False,
                help="N'afficher que les questions où un module pré-retrieval a transformé la query (acronym, HyDE...)",
            )

        # Apply filters
        if outcome_filter:
            pq_df = pq_df[pq_df["Outcome"].isin(outcome_filter)]
        if detail_tags:
            pq_df = pq_df[pq_df["Tags"].apply(lambda t: any(tag in t for tag in detail_tags))]
        if query_mod_filter and "Query modifiée" in pq_df.columns:
            pq_df = pq_df[pq_df["Query modifiée"] == "✅"]

        st.caption(f"{len(pq_df)} questions affichées")

        if "Outcome" in pq_df.columns and not pq_df.empty:
            oc = pq_df["Outcome"].value_counts()
            c1, c2 = st.columns([1, 2])
            with c1:
                st.dataframe(oc, width="stretch")
            with c2:
                st.plotly_chart(px.pie(values=oc.values, names=oc.index, title="Outcomes", height=300), width="stretch", key="outcome_pie")

        st.dataframe(pq_df, width="stretch", hide_index=True, height=600)
        st.download_button("📥 CSV", pq_df.to_csv(index=False), "pipeline_eval.csv", "text/csv")


with tab4:
    st.header("🔬 Ablation Matrix")
    results = st.session_state.get("pipe_eval_results")
    if not results or not results.get("aggregate"):
        st.info("Lancez d'abord une évaluation.")
    elif len(results["configs"]) < 2:
        st.warning("Il faut au moins 2 configs.")
    else:
        baseline = results["configs"][0].name
        st.info(f"**Baseline** : {baseline}")

        abl_df = ablation_matrix(results)
        if not abl_df.empty:
            st.dataframe(abl_df, width="stretch", hide_index=True)

        fig = ablation_heatmap(results)
        if fig:
            st.plotly_chart(fig, width="stretch")

        st.subheader("📝 Résumé des impacts")
        baseline_agg = results["aggregate"].get(baseline, {})
        for cfg in results["configs"][1:]:
            agg = results["aggregate"].get(cfg.name, {})
            mrr_d = (agg.get("mrr", 0) or 0) - (baseline_agg.get("mrr", 0) or 0)
            r10_d = (agg.get("recall@10", 0) or 0) - (baseline_agg.get("recall@10", 0) or 0)
            t_d = (agg.get("avg_total_time_ms", 0) or 0) - (baseline_agg.get("avg_total_time_ms", 0) or 0)
            st.markdown(f"""
**{cfg.name}** vs {baseline} :
- {"🟢" if mrr_d > 0 else "🔴" if mrr_d < 0 else "⚪"} MRR : {"+" if mrr_d >= 0 else ""}{mrr_d:.4f}
- {"🟢" if r10_d > 0 else "🔴" if r10_d < 0 else "⚪"} Recall@10 : {"+" if r10_d >= 0 else ""}{r10_d * 100:.1f}pp
- {"🟢" if t_d < 0 else "🔴" if t_d > 0 else "⚪"} Latence : {"+" if t_d >= 0 else ""}{t_d:.0f}ms
""")


with tab5:
    st.header("📊 Sources & Selector")
    results = st.session_state.get("pipe_eval_results")
    if not results or not results.get("aggregate"):
        st.info("Lancez d'abord une évaluation.")
    else:
        configs_s = [c for c in results["configs"] if c.name in results["aggregate"]]
        aliases_s = _build_aliases(configs_s)

        # ── 1. Aggregate source distribution per stage per config ──
        st.subheader("📊 Distribution des sources par étape")

        stages = ["after_chunks", "after_sections", "after_selector"]
        stage_labels = {
            "after_chunks": "Après Chunk Retrieval",
            "after_sections": "Après Section Rerank",
            "after_selector": "Après LLM Selector",
        }

        for cfg in configs_s:
            a = aliases_s[cfg.name]
            st.markdown(f"#### {a} — {cfg.name}")

            # Aggregate source counts across all questions
            agg_dist = {stage: Counter() for stage in stages}
            n_valid = 0
            for q in results.get("per_question", []):
                data = q.get(cfg.name)
                if data and not data.get("skipped"):
                    n_valid += 1
                    src_dist = data.get("source_dist", {})
                    for stage in stages:
                        for pub, cnt in src_dist.get(stage, {}).items():
                            agg_dist[stage][pub] += cnt

            if n_valid == 0:
                st.caption("Aucune donnée")
                continue

            # Build comparison table: stage × publisher
            all_publishers = sorted(set().union(*(d.keys() for d in agg_dist.values())))
            dist_rows = []
            for stage in stages:
                if not agg_dist[stage]:
                    continue
                row = {"Étape": stage_labels[stage]}
                total = sum(agg_dist[stage].values())
                for pub in all_publishers:
                    cnt = agg_dist[stage].get(pub, 0)
                    pct = cnt / total * 100 if total > 0 else 0
                    row[pub] = f"{cnt} ({pct:.0f}%)"
                row["Total"] = str(total)
                row["Moy/question"] = f"{total / n_valid:.1f}"
                dist_rows.append(row)

            if dist_rows:
                st.dataframe(pd.DataFrame(dist_rows), width="stretch", hide_index=True)

            # Stacked bar chart for this config
            bar_data = []
            for stage in stages:
                if not agg_dist[stage]:
                    continue
                total = sum(agg_dist[stage].values())
                for pub, cnt in agg_dist[stage].items():
                    bar_data.append(
                        {
                            "Étape": stage_labels[stage],
                            "Publisher": pub,
                            "Moy/question": cnt / n_valid if n_valid > 0 else 0,
                        }
                    )
            if bar_data:
                fig = px.bar(
                    pd.DataFrame(bar_data),
                    x="Étape",
                    y="Moy/question",
                    color="Publisher",
                    barmode="stack",
                    title=f"Sources moyennes par question — {a}",
                    height=300,
                )
                st.plotly_chart(fig, width="stretch")

        # ── 2. LLM Selector detail ──
        selector_configs = [c for c in configs_s if c.enable_llm_selector]
        if selector_configs:
            st.divider()
            st.subheader("🔍 Détail LLM Selector")

            for cfg in selector_configs:
                a = aliases_s[cfg.name]
                st.markdown(f"#### {a} — {cfg.name}")

                items_input = []
                items_selected = []
                all_rejected_count = 0

                sel_rows = []
                for q in results.get("per_question", []):
                    data = q.get(cfg.name)
                    if not data or data.get("skipped"):
                        continue
                    sd = data.get("selector_detail")
                    if not sd:
                        continue

                    if sd.get("n_items_input") is not None:
                        items_input.append(sd["n_items_input"])
                    if sd.get("n_items_selected") is not None:
                        items_selected.append(sd["n_items_selected"])
                    if sd.get("all_rejected"):
                        all_rejected_count += 1

                    sel_rows.append(
                        {
                            "Question": q["question"][:60],
                            "Gold": q.get("gold_sources", ""),
                            "Input": sd.get("n_items_input", ""),
                            "Selected": sd.get("n_items_selected", ""),
                            "Rejeté": "❌" if sd.get("all_rejected") else "",
                            "Reason": (sd.get("reason") or "")[:100],
                        }
                    )

                n_sel = len(sel_rows)
                if n_sel > 0:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Questions", n_sel)
                    c2.metric("Tout rejeté", f"{all_rejected_count} ({all_rejected_count / n_sel * 100:.0f}%)")
                    if items_input and items_selected:
                        avg_in = sum(items_input) / len(items_input)
                        avg_sel = sum(items_selected) / len(items_selected)
                        c3.metric("Ratio sélection", f"{avg_sel / avg_in * 100:.0f}%")
                        st.caption(f"Moy items en entrée: {avg_in:.1f} → sélectionnés: {avg_sel:.1f}")

                    st.dataframe(pd.DataFrame(sel_rows), width="stretch", hide_index=True, height=400)
