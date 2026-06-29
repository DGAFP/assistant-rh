"""
🧪 Chunking Evaluation - Comparaison des stratégies de chunking

Compare retrieval quality across chunking strategies (Data Engineer vs V3)
using document-level recall metrics on the common_corpus goldset.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st
from assistant_rh_rag_pipeline.db_helpers import get_dsn as get_app_dsn
from dotenv import load_dotenv
from psycopg.rows import dict_row

from src.ui.admin_auth import require_admin, show_admin_badge

load_dotenv()

require_admin()
show_admin_badge()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Chunking Evaluation",
    page_icon="🧪",
    layout="wide",
)

st.title("🧪 Chunking Strategy Evaluation")
st.caption("Comparaison des stratégies de chunking — Recall, MRR, NDCG sur le corpus commun")


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_dsn() -> str:
    """Get database DSN with tunnel support."""
    tunnel_dsn = os.getenv("TUNNEL_DSN")
    if tunnel_dsn:
        return tunnel_dsn
    try:
        return get_app_dsn()
    except RuntimeError:
        st.error("Aucune connexion DB configurée (SCW_POSTGRES_DSN / APP_POSTGRES_DSN / STREAMLIT_POSTGRES_DSN / TUNNEL_DSN)")
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


def gold_sources_overlap(value, short_ids) -> bool:
    source_keys = {source.lower() for source in parse_gold_sources(value)}
    short_id_keys = {str(short_id or "").strip().lower() for short_id in short_ids if str(short_id or "").strip()}
    return bool(source_keys & short_id_keys)


def get_connection():
    """Get a shared DB connection, reusing if possible."""
    conn = st.session_state.get("_chunk_eval_conn")
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            st.session_state["_chunk_eval_conn"] = None

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(1 * attempt)
            conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
            st.session_state["_chunk_eval_conn"] = conn
            return conn
        except Exception as e:
            if attempt == 2:
                raise e
    raise RuntimeError("Could not connect to DB")


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TableConfig:
    """Configuration for a single table in a chunking strategy."""
    table: str
    embed_col: str
    text_col: str
    id_col: str
    short_id_mode: str  # "column" = short_id is a column, "join_documents" = via rag_documents
    extra_cols: str = ""  # additional SELECT columns
    short_id_col: str = "short_id"  # column name used as short_id (e.g. "number" for V2 tables)


@dataclass
class ChunkingStrategy:
    """A chunking strategy to evaluate."""
    key: str
    name: str
    description: str
    color: str
    tables: List[TableConfig] = field(default_factory=list)


# ── Define strategies ────────────────────────────────────────────────────────

STRATEGY_DE = ChunkingStrategy(
    key="de",
    name="Chunking V1",
    description="Chunks basiques, embeddings non-contextualisés (Albert/BGE-M3), texte brut",
    color="#FF6B6B",
    tables=[
        TableConfig(
            table="rag_chunks_matte",
            embed_col="embedding_m3",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
        TableConfig(
            table="rag_chunks_service_public",
            embed_col="embedding_m3",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
    ],
)

STRATEGY_DE_CTX = ChunkingStrategy(
    key="de_ctx",
    name="Chunking V1 + Ctx Embed",
    description="Mêmes chunks V1, mais embeddings contextualisés (Document + Section + text)",
    color="#FFA726",
    tables=[
        TableConfig(
            table="rag_chunks_matte",
            embed_col="embedding_ctx",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
        TableConfig(
            table="rag_chunks_service_public",
            embed_col="embedding_ctx",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
    ],
)

STRATEGY_V3 = ChunkingStrategy(
    key="v3",
    name="Chunking V3",
    description="Contextualized embeddings, extraction markdown, dynamic chunking (chunks → sections → documents)",
    color="#4ECDC4",
    tables=[
        TableConfig(
            table="rag_chunks_test",
            embed_col="embedding",
            text_col="chunk_markdown",
            id_col="chunk_id",
            short_id_mode="join_documents",
        ),
    ],
)

STRATEGY_V3_RAW = ChunkingStrategy(
    key="v3_raw",
    name="Chunking V3 Raw Embed",
    description="Mêmes chunks V3, mais embeddings bruts (chunk_markdown sans contexte)",
    color="#81C784",
    tables=[
        TableConfig(
            table="rag_chunks_test",
            embed_col="embedding_raw",
            text_col="chunk_markdown",
            id_col="chunk_id",
            short_id_mode="join_documents",
        ),
    ],
)

STRATEGY_V3_RAW_TEXT = ChunkingStrategy(
    key="v3_raw_text",
    name="Chunking V3 Raw Text",
    description="Chunks V3, embeddings sur texte pur (markdown strippé, sans contexte)",
    color="#4DB6AC",
    tables=[
        TableConfig(
            table="rag_chunks_test",
            embed_col="embedding_raw_text",
            text_col="chunk_markdown",
            id_col="chunk_id",
            short_id_mode="join_documents",
        ),
    ],
)

STRATEGY_V2_ORIGINAL = ChunkingStrategy(
    key="v2_original",
    name="Chunking V2 (Original)",
    description="Tables originales pre-DE: rag_chunks_matte_original + rag_chunks_fiches_sp, embeddings BGE-M3",
    color="#AB47BC",
    tables=[
        TableConfig(
            table="rag_chunks_matte_original",
            embed_col="embedding",
            text_col="chunk_text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, number",
            short_id_col="number",
        ),
        TableConfig(
            table="rag_chunks_fiches_sp",
            embed_col="embedding",
            text_col="chunk_text",
            id_col="chunk_id",
            short_id_mode="column",
            extra_cols=", title, full_title, source_name, number, url",
            short_id_col="number",
        ),
    ],
)

STRATEGY_HF_SP = ChunkingStrategy(
    key="hf_sp",
    name="Chunking HF Service-Public",
    description="Chunks officiels AgentPublic/service-public (HuggingFace), embeddings BGE-M3",
    color="#FFB74D",
    tables=[
        TableConfig(
            table="rag_chunks_hf_sp",
            embed_col="embedding",
            text_col="chunk_text",
            id_col="chunk_id",
            short_id_mode="column",
            extra_cols=", title, short_id, url, theme",
        ),
    ],
)

STRATEGY_DE_MAPPED = ChunkingStrategy(
    key="de_mapped",
    name="DE Mapped (section_id)",
    description="Chunks DE (MATTE+SP) mappés aux sections V3 via section_id, embeddings BGE-M3",
    color="#EF5350",
    tables=[
        TableConfig(
            table="rag_chunks_matte",
            embed_col="embedding_m3",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
        TableConfig(
            table="rag_chunks_service_public",
            embed_col="embedding_m3",
            text_col="text",
            id_col="hash_id",
            short_id_mode="column",
            extra_cols=", source_name, source, role, short_id",
        ),
    ],
)

STRATEGY_HYBRID = ChunkingStrategy(
    key="hybrid",
    name="Hybrid (V3 + DE)",
    description="V3 raw embed + DE mappés combinés, embeddings mixtes, fusion par score",
    color="#7E57C2",
    tables=[],  # Special: handled by run_strategy_retrieval via mixed mode
)

ALL_STRATEGIES = [STRATEGY_DE, STRATEGY_DE_CTX, STRATEGY_V3, STRATEGY_V3_RAW, STRATEGY_V3_RAW_TEXT, STRATEGY_V2_ORIGINAL, STRATEGY_HF_SP, STRATEGY_DE_MAPPED, STRATEGY_HYBRID]
STRATEGY_MAP = {s.key: s for s in ALL_STRATEGIES}

# Color map for charts
STRATEGY_COLORS = {s.key: s.color for s in ALL_STRATEGIES}


# ═══════════════════════════════════════════════════════════════════════════════
# GOLDSET DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class GoldsetConfig:
    """Configuration for a goldset."""
    key: str
    name: str
    description: str
    match_mode: str  # "document" (match by short_id) or "chunk" (match by gold_chunk_id)
    filter_sql: str  # WHERE clause to select questions
    gold_chunk_field: str = ""  # JSON path in comment column for chunk-level matching


GOLDSETS = {
    "common_corpus": GoldsetConfig(
        key="common_corpus",
        name="Common Corpus (doc-level)",
        description="224 questions document-level, tag common_corpus",
        match_mode="document",
        filter_sql="'common_corpus' = ANY(tags)",
    ),
    "common_corpus_chunk": GoldsetConfig(
        key="common_corpus_chunk",
        name="Common Corpus (chunk-level, neutre)",
        description="Questions doc-level Service-Public mappées au chunk V1+V3 par LLM (SANS biais de chunking). Utiliser filtre Source: Service-Public recommandé.",
        match_mode="chunk",
        filter_sql="'common_corpus' = ANY(tags)",
        gold_chunk_field="gold_chunk_id",
    ),
    "chunk_level_v1": GoldsetConfig(
        key="chunk_level_v1",
        name="Chunk Level V1 (biaisé V1)",
        description="~183 questions générées depuis chunks V1, mappées vers V3",
        match_mode="chunk",
        filter_sql="goldset_name = 'chunk_level_v1'",
        gold_chunk_field="gold_chunk_id",
    ),
    "chunk_level_v3": GoldsetConfig(
        key="chunk_level_v3",
        name="Chunk Level V3 (biaisé V3)",
        description="Questions générées depuis chunks V3, mappées vers V1",
        match_mode="chunk",
        filter_sql="goldset_name = 'chunk_level_v3'",
        gold_chunk_field="gold_chunk_id",
    ),
    "fiches_sp_subset": GoldsetConfig(
        key="fiches_sp_subset",
        name="Fiches SP Subset (doc-level)",
        description="Toutes questions sur les 48 docs fiches_sp — comparaison V2/HF/V3 équitable",
        match_mode="document",
        filter_sql="gold_sources IN (SELECT DISTINCT number FROM rag_chunks_fiches_sp WHERE number IS NOT NULL)",
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# QUESTION LOADING
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def load_goldset_questions(goldset_key: str, limit: Optional[int] = None) -> pd.DataFrame:
    """Load questions for a given goldset with precomputed embeddings."""
    gc = GOLDSETS[goldset_key]
    sql = f"""
        SELECT id, question, gold_answer, gold_sources, theme, comment, embedding_albert
        FROM goldset_questions_v2
        WHERE {gc.filter_sql}
          AND embedding_albert IS NOT NULL
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Extract gold_chunk_ids from comment JSON for chunk-level goldsets
    if gc.match_mode == "chunk":
        def _extract_chunk_meta(comment):
            if not comment:
                return {}
            try:
                data = json.loads(comment) if isinstance(comment, str) else comment
                return {
                    "gold_chunk_id": data.get("gold_chunk_id"),
                    "gold_chunk_id_v3": data.get("gold_chunk_id_v3"),
                    "v1_confidence": data.get("v1_confidence"),
                    "v3_confidence": data.get("v3_confidence"),
                    "v3_score": data.get("v3_score"),
                    "mapping_error": data.get("mapping_error", False),
                }
            except (json.JSONDecodeError, TypeError):
                return {}

        chunk_meta = df["comment"].apply(_extract_chunk_meta)
        df["gold_chunk_id"] = chunk_meta.apply(lambda x: x.get("gold_chunk_id"))
        df["gold_chunk_id_v3"] = chunk_meta.apply(lambda x: x.get("gold_chunk_id_v3"))
        df["v1_confidence"] = chunk_meta.apply(lambda x: x.get("v1_confidence"))
        df["v3_confidence"] = chunk_meta.apply(lambda x: x.get("v3_confidence"))
        df["v3_score"] = chunk_meta.apply(lambda x: x.get("v3_score"))
        df["mapping_error"] = chunk_meta.apply(lambda x: x.get("mapping_error", False))

    return df


@st.cache_data(ttl=600)
def load_goldset_short_ids(goldset_key: str) -> List[str]:
    """Load the set of short_ids in a goldset."""
    gc = GOLDSETS[goldset_key]
    sql = f"""
        SELECT DISTINCT gold_sources
        FROM goldset_questions_v2
        WHERE {gc.filter_sql}
          AND gold_sources IS NOT NULL
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    short_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for short_id in parse_gold_sources(row["gold_sources"]):
            if short_id in seen:
                continue
            seen.add(short_id)
            short_ids.append(short_id)
    return short_ids


# ═══════════════════════════════════════════════════════════════════════════════
# RETRIEVAL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

# Publisher filter mapping: DE table name -> publisher value
DE_TABLE_PUBLISHER = {
    "rag_chunks_matte": "MATTE",
    "rag_chunks_service_public": "Service-Public",
    "rag_chunks_matte_original": "MATTE",
    "rag_chunks_fiches_sp": "Service-Public",
    "rag_chunks_hf_sp": "Service-Public",
}

PUBLISHER_OPTIONS = ["Tous", "Service-Public", "MATTE"]


def search_de_table(
    cur,
    table_cfg: TableConfig,
    query_embedding: List[float],
    top_k: int = 50,
    filter_short_ids: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Semantic search on a DE table (rag_chunks_matte, rag_chunks_service_public, etc.).
    Optionally filters to common_corpus documents via short_id_col.
    """
    sid_col = table_cfg.short_id_col  # e.g. "short_id" or "number"
    where_filter = ""
    params: list = []

    if filter_short_ids:
        where_filter = f"AND {table_cfg.table}.{sid_col} = ANY(%s)"
        params = [query_embedding, filter_short_ids, query_embedding, top_k]
    else:
        params = [query_embedding, query_embedding, top_k]

    # Alias short_id_col as "short_id" so downstream code always sees "short_id"
    sid_alias = f", {sid_col} AS short_id" if sid_col != "short_id" else ""

    sql = f"""
        SELECT
            {table_cfg.id_col}::text as chunk_id,
            {table_cfg.text_col} as text,
            1 - ({table_cfg.embed_col} <=> %s::vector) as score
            {table_cfg.extra_cols}{sid_alias}
        FROM {table_cfg.table}
        WHERE {table_cfg.embed_col} IS NOT NULL {where_filter}
        ORDER BY {table_cfg.embed_col} <=> %s::vector
        LIMIT %s
    """

    cur.execute(sql, params)
    results = cur.fetchall()

    for r in results:
        r["table_source"] = table_cfg.table
        r["strategy"] = "de"
        # short_id comes from extra_cols or sid_alias

    return [dict(r) for r in results]


def search_v3_table(
    cur,
    query_embedding: List[float],
    top_k: int = 50,
    filter_short_ids: Optional[List[str]] = None,
    embed_col: str = "embedding",
    strategy_key: str = "v3",
    publisher_filter: Optional[str] = None,
) -> List[Dict]:
    """
    Semantic search on V3 tables (rag_chunks_test + rag_chunk_embeddings + rag_documents).
    Optionally filters to common_corpus documents via rag_documents.short_id.
    Optionally filters by publisher (e.g. 'MATTE' or 'Service-Public').
    embed_col: column in rag_chunk_embeddings to use (e.g. 'embedding' for ctx, 'embedding_raw' for raw).
    """
    embed_ref = f"e.{embed_col}"
    where_clauses = []
    params: list = [query_embedding]  # first %s for score calculation

    if filter_short_ids:
        where_clauses.append("d.short_id = ANY(%s)")
        params.append(filter_short_ids)
    if publisher_filter:
        where_clauses.append("d.publisher = %s")
        params.append(publisher_filter)

    params.append(query_embedding)  # for ORDER BY
    params.append(top_k)  # for LIMIT

    where_extra = ""
    if where_clauses:
        where_extra = "AND " + " AND ".join(where_clauses)

    sql = f"""
        SELECT
            c.chunk_id::text as chunk_id,
            c.chunk_markdown as text,
            c.token_count,
            1 - ({embed_ref} <=> %s::vector) as score,
            c.section_id::text as section_id,
            s.heading as section_heading,
            s.section_markdown,
            s.heading_path,
            s.level as section_level,
            s.token_count as section_token_count,
            d.title as source_name,
            d.publisher as source,
            d.short_id
        FROM rag_chunks_test c
        JOIN rag_chunk_embeddings e ON c.chunk_id = e.chunk_id AND e.embedding_model = 'albert'
        JOIN rag_sections s ON c.section_id = s.section_id
        JOIN rag_documents d ON c.doc_id = d.doc_id
        WHERE {embed_ref} IS NOT NULL {where_extra}
        ORDER BY {embed_ref} <=> %s::vector
        LIMIT %s
    """

    cur.execute(sql, params)
    results = cur.fetchall()

    for r in results:
        r["table_source"] = "rag_chunks_test"
        r["strategy"] = strategy_key

    return [dict(r) for r in results]


def search_de_table_mapped(
    cur,
    table_cfg: TableConfig,
    query_embedding: List[float],
    top_k: int = 50,
    filter_short_ids: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Semantic search on a DE table, but only chunks with section_id mapped.
    Joins with rag_sections and rag_documents to get section metadata + short_id.
    """
    where_extra = ""
    params: list = [query_embedding]

    if filter_short_ids:
        where_extra = "AND d.short_id = ANY(%s)"
        params.append(filter_short_ids)

    params.append(query_embedding)  # for ORDER BY
    params.append(top_k)

    sql = f"""
        SELECT
            de.{table_cfg.id_col}::text as chunk_id,
            de.{table_cfg.text_col} as text,
            1 - (de.{table_cfg.embed_col} <=> %s::vector) as score,
            de.section_id::text as section_id,
            s.heading as section_heading,
            s.section_markdown,
            s.heading_path,
            s.level as section_level,
            s.token_count as section_token_count,
            d.title as source_name,
            d.publisher as source,
            d.short_id
        FROM {table_cfg.table} de
        JOIN rag_sections s ON de.section_id = s.section_id
        JOIN rag_documents d ON s.doc_id = d.doc_id
        WHERE de.section_id IS NOT NULL
          AND de.{table_cfg.embed_col} IS NOT NULL
          {where_extra}
        ORDER BY de.{table_cfg.embed_col} <=> %s::vector
        LIMIT %s
    """

    cur.execute(sql, params)
    results = cur.fetchall()

    for r in results:
        r["table_source"] = table_cfg.table
        r["strategy"] = "de_mapped"

    return [dict(r) for r in results]


def run_strategy_retrieval(
    cur,
    strategy: ChunkingStrategy,
    query_embedding: List[float],
    top_k: int = 50,
    filter_short_ids: Optional[List[str]] = None,
    publisher_filter: Optional[str] = None,
) -> List[Dict]:
    """
    Run retrieval for a given strategy. Merges results from all tables and sorts by score.
    publisher_filter: 'MATTE', 'Service-Public', or None for all.
    For DE strategies, filters tables by publisher. For V3, adds WHERE on d.publisher.
    """
    all_chunks = []

    # ── HYBRID strategy: combine V3 raw + DE mapped ──
    if strategy.key == "hybrid":
        per_source_k = top_k
        # V3 part: use raw embed
        v3_chunks = search_v3_table(
            cur, query_embedding, top_k=per_source_k,
            filter_short_ids=filter_short_ids,
            embed_col="embedding_raw",
            strategy_key="hybrid",
            publisher_filter=publisher_filter,
        )
        all_chunks.extend(v3_chunks)
        # DE mapped part
        for table_cfg in STRATEGY_DE_MAPPED.tables:
            if publisher_filter:
                table_pub = DE_TABLE_PUBLISHER.get(table_cfg.table)
                if table_pub and table_pub != publisher_filter:
                    continue
            chunks = search_de_table_mapped(cur, table_cfg, query_embedding, top_k=per_source_k, filter_short_ids=filter_short_ids)
            all_chunks.extend(chunks)
        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_chunks = all_chunks[:top_k]
        return all_chunks

    # ── DE MAPPED strategy: only mapped chunks with section JOIN ──
    if strategy.key == "de_mapped":
        for table_cfg in strategy.tables:
            if publisher_filter:
                table_pub = DE_TABLE_PUBLISHER.get(table_cfg.table)
                if table_pub and table_pub != publisher_filter:
                    continue
            chunks = search_de_table_mapped(cur, table_cfg, query_embedding, top_k=top_k, filter_short_ids=filter_short_ids)
            all_chunks.extend(chunks)
        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_chunks = all_chunks[:top_k]
        return all_chunks

    # Check if this is a V3-type strategy (uses rag_chunks_test with join_documents)
    is_v3 = any(t.short_id_mode == "join_documents" for t in strategy.tables)

    if is_v3:
        # V3 strategy: use search_v3_table with the configured embed_col + publisher filter
        embed_col = strategy.tables[0].embed_col if strategy.tables else "embedding"
        all_chunks = search_v3_table(
            cur, query_embedding, top_k=top_k,
            filter_short_ids=filter_short_ids,
            embed_col=embed_col,
            strategy_key=strategy.key,
            publisher_filter=publisher_filter,
        )
    else:
        # DE strategy: search each table, skip tables not matching publisher filter
        for table_cfg in strategy.tables:
            # Publisher filter: skip tables that don't match
            if publisher_filter:
                table_pub = DE_TABLE_PUBLISHER.get(table_cfg.table)
                if table_pub and table_pub != publisher_filter:
                    continue  # skip this table
            chunks = search_de_table(cur, table_cfg, query_embedding, top_k=top_k, filter_short_ids=filter_short_ids)
            all_chunks.extend(chunks)

        # Sort merged results by score desc and keep top_k
        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_chunks = all_chunks[:top_k]

    return all_chunks


# ═══════════════════════════════════════════════════════════════════════════════
# RERANKER
# ═══════════════════════════════════════════════════════════════════════════════

def rerank_chunks(
    query: str,
    chunks: List[Dict],
    top_k: int = 20,
) -> List[Dict]:
    """Rerank chunks using Albert API reranker."""
    if not chunks:
        return chunks

    texts = [c.get("text", "")[:2000] for c in chunks]

    from assistant_rh_rag_pipeline.reranker import AlbertReranker
    reranker = AlbertReranker(timeout=10)
    ranked = reranker.rerank(query, texts, top_k=top_k)
    reranked = []
    for idx, score in ranked:
        chunk = chunks[idx].copy()
        chunk["rerank_score"] = score
        chunk["original_rank"] = idx
        reranked.append(chunk)
    return reranked


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION EXPANSION + RERANK
# ═══════════════════════════════════════════════════════════════════════════════

def expand_chunks_to_sections(
    cur,
    chunks: List[Dict],
) -> List[Dict]:
    """
    Expand chunk results to section-level by grouping chunks that share a section_id.

    For chunks from search_de_table_mapped or search_v3_table, section metadata is
    already present. For chunks without section_id (basic DE search), they pass through
    unchanged.

    Returns section-level results sorted by aggregated score (max chunk score per section).
    Each result has short_id and section_markdown for downstream matching and reranking.
    """
    if not chunks:
        return chunks

    section_map = {}  # section_id -> best chunk (with section data)
    no_section = []   # chunks without section_id

    for chunk in chunks:
        sid = chunk.get("section_id")
        if not sid:
            # No section mapping: if the chunk came from V3, try to fetch section_id
            chunk_id = chunk.get("chunk_id")
            if chunk.get("table_source") == "rag_chunks_test" and chunk_id:
                cur.execute(
                    "SELECT section_id::text FROM rag_chunks_test WHERE chunk_id = %s",
                    [chunk_id]
                )
                row = cur.fetchone()
                if row and row.get("section_id"):
                    sid = row["section_id"]
                    chunk["section_id"] = sid

        if sid:
            if sid not in section_map or chunk.get("score", 0) > section_map[sid].get("score", 0):
                section_map[sid] = chunk.copy()
        else:
            no_section.append(chunk)

    # For sections that don't have section_markdown yet, fetch it
    for sid, sec_chunk in section_map.items():
        if not sec_chunk.get("section_markdown"):
            cur.execute("""
                SELECT s.section_markdown, s.heading as section_heading,
                       s.heading_path, s.level as section_level,
                       s.token_count as section_token_count,
                       d.short_id, d.title as source_name, d.publisher as source
                FROM rag_sections s
                JOIN rag_documents d ON s.doc_id = d.doc_id
                WHERE s.section_id = %s::uuid
            """, [sid])
            row = cur.fetchone()
            if row:
                for k, v in dict(row).items():
                    if v is not None:
                        sec_chunk[k] = v

    # Combine: sections first (by score), then orphan chunks
    sections = list(section_map.values())
    sections.sort(key=lambda x: x.get("score", 0), reverse=True)
    return sections + no_section


def rerank_sections(
    query: str,
    sections: List[Dict],
    top_k: int = 20,
) -> List[Dict]:
    """Rerank section-level results using section_markdown content."""
    if not sections:
        return sections

    # Build rerank texts: heading + section_markdown preview
    texts = []
    for s in sections:
        heading = s.get("section_heading", "")
        markdown = s.get("section_markdown", s.get("text", ""))
        texts.append(f"# {heading}\n\n{markdown[:2000]}")

    from assistant_rh_rag_pipeline.reranker import AlbertReranker
    reranker = AlbertReranker(timeout=10)
    ranked = reranker.rerank(query, texts, top_k=top_k)

    reranked = []
    for idx, score in ranked:
        section = sections[idx].copy()
        section["rerank_score"] = score
        section["original_rank"] = idx
        reranked.append(section)
    return reranked


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

def _is_hit(chunk: Dict, gold_sources: str, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> bool:
    """Check if a chunk matches the gold reference.
    
    match_mode:
      - "document": match by short_id vs gold_sources
      - "chunk": match by chunk_id (hash_id) vs gold_chunk_id
    """
    if match_mode == "chunk":
        if not gold_chunk_id:
            return False
        chunk_id = chunk.get("chunk_id") or chunk.get("hash_id") or ""
        return chunk_id.strip().lower() == gold_chunk_id.strip().lower()
    else:
        chunk_short_id = chunk.get("short_id")
        return gold_sources_match(gold_sources, chunk_short_id)


def compute_recall_at_k(chunks: List[Dict], gold_sources: str, k: int, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> int:
    """Returns 1 if any chunk in top-k is from the gold document/chunk, else 0."""
    for chunk in chunks[:k]:
        if _is_hit(chunk, gold_sources, match_mode, gold_chunk_id):
            return 1
    return 0


def compute_mrr(chunks: List[Dict], gold_sources: str, max_k: int = 100, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> float:
    """Mean Reciprocal Rank: 1/rank of first relevant chunk."""
    for i, chunk in enumerate(chunks[:max_k]):
        if _is_hit(chunk, gold_sources, match_mode, gold_chunk_id):
            return 1.0 / (i + 1)
    return 0.0


def compute_ndcg_at_k(chunks: List[Dict], gold_sources: str, k: int, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> float:
    """NDCG@K with binary relevance."""
    dcg = 0.0
    for i, chunk in enumerate(chunks[:k]):
        if _is_hit(chunk, gold_sources, match_mode, gold_chunk_id):
            dcg += 1.0 / math.log2(i + 2)  # i+2 because log2(1) = 0

    # Ideal DCG: all relevant docs at the top
    # Count total relevant in top-k
    n_rel = sum(1 for c in chunks[:k] if _is_hit(c, gold_sources, match_mode, gold_chunk_id))
    if n_rel == 0:
        return 0.0

    idcg = sum(1.0 / math.log2(j + 2) for j in range(n_rel))
    return dcg / idcg if idcg > 0 else 0.0


def compute_first_hit_rank(chunks: List[Dict], gold_sources: str, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> Optional[int]:
    """Return rank (1-based) of first relevant chunk, or None if not found."""
    for i, chunk in enumerate(chunks):
        if _is_hit(chunk, gold_sources, match_mode, gold_chunk_id):
            return i + 1
    return None


def compute_hit_count(chunks: List[Dict], gold_sources: str, k: int, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> int:
    """Count how many chunks in top-k match the gold document/chunk."""
    return sum(1 for c in chunks[:k] if _is_hit(c, gold_sources, match_mode, gold_chunk_id))


K_VALUES = [1, 3, 5, 10, 20, 50]


def compute_all_metrics(chunks: List[Dict], gold_sources: str, match_mode: str = "document", gold_chunk_id: Optional[str] = None) -> Dict:
    """Compute all metrics for one question + one strategy."""
    metrics = {}
    for k in K_VALUES:
        metrics[f"recall@{k}"] = compute_recall_at_k(chunks, gold_sources, k, match_mode, gold_chunk_id)
        metrics[f"ndcg@{k}"] = compute_ndcg_at_k(chunks, gold_sources, k, match_mode, gold_chunk_id)
        metrics[f"hits@{k}"] = compute_hit_count(chunks, gold_sources, k, match_mode, gold_chunk_id)
    metrics["mrr"] = compute_mrr(chunks, gold_sources, match_mode=match_mode, gold_chunk_id=gold_chunk_id)
    metrics["first_hit_rank"] = compute_first_hit_rank(chunks, gold_sources, match_mode, gold_chunk_id)
    return metrics


# ═══════════════════════════════════════════════════════════════════════════════
# RUN EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def run_evaluation(
    strategies: List[ChunkingStrategy],
    goldset_key: str = "common_corpus",
    top_k: int = 50,
    max_questions: Optional[int] = None,
    enable_rerank: bool = False,
    rerank_top_k: int = 20,
    filter_corpus: bool = True,
    v3_confidence_filter: Optional[List[str]] = None,
    exclude_mapping_errors: bool = False,
    publisher_filter: Optional[str] = None,
    require_both_mappings: bool = False,
    enable_section_expansion: bool = False,
    section_rerank_top_k: int = 15,
) -> Dict:
    """
    Run the full evaluation pipeline across all strategies.
    Returns a dict with per-strategy, per-question results and aggregate metrics.
    publisher_filter: 'MATTE', 'Service-Public', or None for all.
    require_both_mappings: if True, only keep questions that have BOTH gold_chunk_id and gold_chunk_id_v3.
    enable_section_expansion: expand chunks to sections + rerank on section_markdown.
    """
    gc = GOLDSETS[goldset_key]
    match_mode = gc.match_mode

    # Load questions
    questions_df = load_goldset_questions(goldset_key, limit=max_questions)
    if questions_df.empty:
        st.error(f"Aucune question '{goldset_key}' trouvée avec embeddings.")
        return {}

    # Filter by publisher: keep only questions whose gold_sources come from the right source
    if publisher_filter:
        def _get_chunk_table(comment):
            if not comment:
                return None
            try:
                data = json.loads(comment) if isinstance(comment, str) else comment
                return data.get("table")
            except (json.JSONDecodeError, TypeError):
                return None

        if match_mode == "chunk":
            # Try chunk table from comment JSON first (works for chunk_level_v1/v3 goldsets)
            questions_df["_chunk_table"] = questions_df["comment"].apply(_get_chunk_table) if "comment" in questions_df.columns else None
            has_table_info = questions_df["_chunk_table"].notna().any() if "_chunk_table" in questions_df.columns else False

            if has_table_info:
                # Filter by table name in comment
                if publisher_filter == "MATTE":
                    questions_df = questions_df[questions_df["_chunk_table"] == "rag_chunks_matte"]
                elif publisher_filter == "Service-Public":
                    questions_df = questions_df[questions_df["_chunk_table"] == "rag_chunks_service_public"]
                questions_df = questions_df.drop(columns=["_chunk_table"])
            else:
                # Fallback: use gold_sources (short_id) → rag_documents.publisher lookup
                # This works for neutral goldsets where questions came from doc-level
                conn = get_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT short_id FROM rag_documents WHERE publisher = %s",
                        [publisher_filter]
                    )
                    pub_short_ids = set(r["short_id"] for r in cur.fetchall())
                if "_chunk_table" in questions_df.columns:
                    questions_df = questions_df.drop(columns=["_chunk_table"])
                questions_df = questions_df[questions_df["gold_sources"].apply(lambda value: gold_sources_overlap(value, pub_short_ids))]
        else:
            # For doc-level goldsets: handled below via publisher-filtered short_ids
            pass

    # Filter by V3 mapping confidence (for chunk-level goldsets)
    n_before_filter = len(questions_df)
    if match_mode == "chunk" and v3_confidence_filter and "v3_confidence" in questions_df.columns:
        questions_df = questions_df[
            questions_df["v3_confidence"].isin(v3_confidence_filter)
            | questions_df["v3_confidence"].isna()  # keep unmapped (DE-only strategies still need them)
        ]
    if match_mode == "chunk" and exclude_mapping_errors and "mapping_error" in questions_df.columns:
        questions_df = questions_df[~questions_df["mapping_error"].fillna(False)]

    # Require both V1 and V3 chunk mappings (ensures same question set for all strategies)
    if match_mode == "chunk" and require_both_mappings:
        n_before_both = len(questions_df)
        has_v1 = questions_df["gold_chunk_id"].notna() if "gold_chunk_id" in questions_df.columns else pd.Series(False, index=questions_df.index)
        has_v3 = questions_df["gold_chunk_id_v3"].notna() if "gold_chunk_id_v3" in questions_df.columns else pd.Series(False, index=questions_df.index)
        questions_df = questions_df[has_v1 & has_v3]
        n_after_both = len(questions_df)
        if n_before_both != n_after_both:
            st.caption(f"🔗 Filtre 'both mappings': {n_before_both} → **{n_after_both}** questions (V1+V3 mappées)")

    # Load short_ids for filtering
    filter_short_ids = load_goldset_short_ids(goldset_key) if filter_corpus else None

    # For doc-level with publisher filter: also filter short_ids to the publisher
    if publisher_filter and filter_short_ids and match_mode == "document":
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT short_id FROM rag_documents WHERE publisher = %s",
                [publisher_filter]
            )
            pub_short_ids = set(r["short_id"] for r in cur.fetchall())
        filter_short_ids = [sid for sid in filter_short_ids if sid in pub_short_ids]
        # Also filter questions to matching gold_sources
        questions_df = questions_df[questions_df["gold_sources"].apply(lambda value: gold_sources_overlap(value, pub_short_ids))]

    # V3 mapping coverage info
    v3_info = ""
    if match_mode == "chunk" and "gold_chunk_id_v3" in questions_df.columns:
        n_v3_mapped = questions_df["gold_chunk_id_v3"].notna().sum()
        v3_info = f" | V3 mapped: **{n_v3_mapped}/{len(questions_df)}**"
        if n_before_filter != len(questions_df):
            v3_info += f" (filtré de {n_before_filter})"

    pub_label = publisher_filter or "Tous"
    both_label = " | Both mappings: **ON**" if require_both_mappings else ""
    sec_label = f" | Section expansion: **ON** (top {section_rerank_top_k})" if enable_section_expansion else ""
    st.info(
        f"**{len(questions_df)}** questions chargées ({gc.name}) | "
        f"Match mode: **{match_mode}** | "
        f"Source: **{pub_label}** | "
        f"Corpus filter: **{'ON' if filter_corpus else 'OFF'}** | "
        f"Top K: **{top_k}** | "
        f"Rerank: **{'ON' if enable_rerank else 'OFF'}**"
        f"{v3_info}{both_label}{sec_label}"
    )

    # Results structure
    results = {
        "config": {
            "top_k": top_k,
            "enable_rerank": enable_rerank,
            "rerank_top_k": rerank_top_k,
            "filter_corpus": filter_corpus,
            "goldset_key": goldset_key,
            "match_mode": match_mode,
            "publisher_filter": publisher_filter,
            "n_questions": len(questions_df),
            "strategies": [s.key for s in strategies],
        },
        "per_question": [],  # List of {question_id, question, gold_sources, strategy_key: {metrics, chunks}}
        "aggregate": {},     # {strategy_key: {metric_name: value}}
    }

    progress = st.progress(0, text="Lancement de l'évaluation...")
    t_start = time.time()

    conn = get_connection()

    with conn.cursor() as cur:
        for i, (_, row) in enumerate(questions_df.iterrows()):
            embedding = row["embedding_albert"]
            if embedding is None:
                continue

            question_text = row["question"]
            gold_sources = row.get("gold_sources", "")
            # Store both DE and V3 gold_chunk_ids for chunk-level matching
            gold_chunk_id_de = row.get("gold_chunk_id") if match_mode == "chunk" else None
            gold_chunk_id_v3 = row.get("gold_chunk_id_v3") if match_mode == "chunk" else None

            progress.progress(
                (i + 1) / len(questions_df),
                text=f"Question {i+1}/{len(questions_df)}: {question_text[:60]}..."
            )

            q_result = {
                "question_id": row["id"],
                "question": question_text,
                "gold_sources": format_gold_sources(gold_sources),
                "gold_chunk_id": gold_chunk_id_de,
                "gold_chunk_id_v3": gold_chunk_id_v3,
                "theme": row.get("theme"),
            }

            for strategy in strategies:
                # Pick the right gold_chunk_id for this strategy
                # V3-type strategies use gold_chunk_id_v3, DE-type use gold_chunk_id
                is_v3_strategy = any(t.short_id_mode == "join_documents" for t in strategy.tables)
                if match_mode == "chunk" and is_v3_strategy:
                    strat_gold_chunk_id = gold_chunk_id_v3
                    # Skip V3 metrics if this question has no V3 mapping
                    if not strat_gold_chunk_id:
                        q_result[strategy.key] = {
                            "raw_metrics": {f"recall@{k}": None for k in K_VALUES},
                            "rerank_metrics": None,
                            "retrieval_time_ms": 0,
                            "rerank_time_ms": 0,
                            "n_chunks": 0,
                            "top_chunks": [],
                            "top_reranked": [],
                            "v3_unmapped": True,
                        }
                        q_result[strategy.key]["raw_metrics"]["mrr"] = None
                        q_result[strategy.key]["raw_metrics"]["first_hit_rank"] = None
                        continue
                else:
                    strat_gold_chunk_id = gold_chunk_id_de
                    # Skip DE metrics if this question has no DE chunk mapping
                    if match_mode == "chunk" and not strat_gold_chunk_id:
                        q_result[strategy.key] = {
                            "raw_metrics": {f"recall@{k}": None for k in K_VALUES},
                            "rerank_metrics": None,
                            "retrieval_time_ms": 0,
                            "rerank_time_ms": 0,
                            "n_chunks": 0,
                            "top_chunks": [],
                            "top_reranked": [],
                            "de_unmapped": True,
                        }
                        q_result[strategy.key]["raw_metrics"]["mrr"] = None
                        q_result[strategy.key]["raw_metrics"]["first_hit_rank"] = None
                        continue

                # ── Retrieval ──
                t0 = time.time()
                chunks = run_strategy_retrieval(
                    cur, strategy, embedding,
                    top_k=top_k,
                    filter_short_ids=filter_short_ids,
                    publisher_filter=publisher_filter,
                )
                retrieval_time = time.time() - t0

                # ── Optional section expansion ──
                if enable_section_expansion and chunks:
                    t0_sec = time.time()
                    try:
                        chunks = expand_chunks_to_sections(cur, chunks)
                        chunks = rerank_sections(question_text, chunks, top_k=section_rerank_top_k)
                    except Exception as e:
                        q_result[f"{strategy.key}_section_expand_error"] = str(e)
                    retrieval_time += time.time() - t0_sec

                # ── Optional rerank ──
                reranked = None
                rerank_time = 0.0
                if enable_rerank and chunks:
                    t0 = time.time()
                    try:
                        reranked = rerank_chunks(question_text, chunks, top_k=rerank_top_k)
                    except Exception as e:
                        reranked = chunks[:rerank_top_k]
                        q_result[f"{strategy.key}_rerank_error"] = str(e)
                    rerank_time = time.time() - t0

                # ── Metrics ──
                raw_metrics = compute_all_metrics(chunks, gold_sources, match_mode, strat_gold_chunk_id)
                rerank_metrics = compute_all_metrics(reranked, gold_sources, match_mode, strat_gold_chunk_id) if reranked else None

                q_result[strategy.key] = {
                    "raw_metrics": raw_metrics,
                    "rerank_metrics": rerank_metrics,
                    "retrieval_time_ms": round(retrieval_time * 1000),
                    "rerank_time_ms": round(rerank_time * 1000),
                    "n_chunks": len(chunks),
                    # Store compact chunk info (no full text)
                    "top_chunks": [
                        {
                            "chunk_id": c.get("chunk_id", ""),
                            "short_id": c.get("short_id", ""),
                            "source_name": c.get("source_name", ""),
                            "source": c.get("source", ""),
                            "score": round(c.get("score", 0), 5),
                            "is_hit": _is_hit(c, gold_sources, match_mode, strat_gold_chunk_id),
                        }
                        for c in chunks[:20]
                    ],
                    "top_reranked": [
                        {
                            "chunk_id": c.get("chunk_id", ""),
                            "short_id": c.get("short_id", ""),
                            "source_name": c.get("source_name", ""),
                            "score": round(c.get("rerank_score", c.get("score", 0)), 5),
                            "is_hit": _is_hit(c, gold_sources, match_mode, strat_gold_chunk_id),
                        }
                        for c in (reranked or [])[:20]
                    ],
                }

            results["per_question"].append(q_result)

    elapsed = time.time() - t_start
    results["config"]["elapsed_s"] = round(elapsed, 1)
    progress.progress(1.0, text=f"Terminé en {elapsed:.1f}s")

    # ── Compute aggregates ──
    for strategy in strategies:
        agg = {}
        # Filter out unmapped questions (V3 or DE) where metrics are None
        valid_questions = [
            q for q in results["per_question"]
            if strategy.key in q
            and not q[strategy.key].get("v3_unmapped")
            and not q[strategy.key].get("de_unmapped")
        ]
        n = len(valid_questions)
        n_v3_unmapped = sum(
            1 for q in results["per_question"]
            if strategy.key in q and q[strategy.key].get("v3_unmapped")
        )
        n_de_unmapped = sum(
            1 for q in results["per_question"]
            if strategy.key in q and q[strategy.key].get("de_unmapped")
        )
        n_unmapped = n_v3_unmapped + n_de_unmapped
        agg["n_evaluated"] = n
        agg["n_unmapped"] = n_unmapped

        if n == 0:
            results["aggregate"][strategy.key] = agg
            continue

        # Raw metrics
        for metric_name in [f"recall@{k}" for k in K_VALUES] + ["mrr"] + [f"ndcg@{k}" for k in K_VALUES]:
            values = [q[strategy.key]["raw_metrics"][metric_name] for q in valid_questions if q[strategy.key]["raw_metrics"].get(metric_name) is not None]
            agg[f"raw_{metric_name}"] = round(np.mean(values), 4) if values else 0.0

        # Rerank metrics
        if enable_rerank:
            for metric_name in [f"recall@{k}" for k in K_VALUES] + ["mrr"] + [f"ndcg@{k}" for k in K_VALUES]:
                values = [
                    q[strategy.key]["rerank_metrics"][metric_name]
                    for q in valid_questions
                    if q[strategy.key].get("rerank_metrics") and q[strategy.key]["rerank_metrics"].get(metric_name) is not None
                ]
                agg[f"rerank_{metric_name}"] = round(np.mean(values), 4) if values else 0.0

        # Latency — raw retrieval
        times = [q[strategy.key]["retrieval_time_ms"] for q in valid_questions]
        agg["raw_avg_latency_ms"] = round(np.mean(times), 1) if times else 0.0

        # Latency — rerank (retrieval + rerank combined)
        if enable_rerank:
            rr_times = [
                q[strategy.key]["retrieval_time_ms"] + q[strategy.key].get("rerank_time_ms", 0)
                for q in valid_questions
            ]
            agg["rerank_avg_latency_ms"] = round(np.mean(rr_times), 1) if rr_times else 0.0

        # First hit rank distribution — raw
        raw_ranks = [
            q[strategy.key]["raw_metrics"]["first_hit_rank"]
            for q in valid_questions
            if q[strategy.key]["raw_metrics"].get("first_hit_rank") is not None
        ]
        agg["raw_avg_first_hit_rank"] = round(np.mean(raw_ranks), 2) if raw_ranks else None
        agg["raw_n_found"] = len(raw_ranks)
        agg["raw_n_missed"] = n - len(raw_ranks)

        # First hit rank distribution — rerank
        if enable_rerank:
            rr_ranks = [
                q[strategy.key]["rerank_metrics"]["first_hit_rank"]
                for q in valid_questions
                if q[strategy.key].get("rerank_metrics") and q[strategy.key]["rerank_metrics"].get("first_hit_rank") is not None
            ]
            agg["rerank_avg_first_hit_rank"] = round(np.mean(rr_ranks), 2) if rr_ranks else None
            agg["rerank_n_found"] = len(rr_ranks)
            agg["rerank_n_missed"] = n - len(rr_ranks)

        results["aggregate"][strategy.key] = agg

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE (save to DB)
# ═══════════════════════════════════════════════════════════════════════════════

def save_eval_to_db(results: Dict, run_name: str) -> Optional[int]:
    """Save evaluation results to retrieval_eval_runs."""
    if not results or not results.get("per_question"):
        return None

    config = results["config"]

    # Build question_details and chunk_details for storage
    question_details = []
    chunk_details = []
    for q in results["per_question"]:
        q_detail = {
            "question_id": q["question_id"],
            "question": q["question"][:120],
            "gold_sources": q.get("gold_sources"),
            "gold_chunk_id": q.get("gold_chunk_id"),
            "theme": q.get("theme"),
        }
        q_chunks = {"question_id": q["question_id"]}

        for s_key in config.get("strategies", []):
            if s_key not in q:
                continue
            s_data = q[s_key]
            q_detail[f"{s_key}_recall@10"] = s_data["raw_metrics"].get("recall@10", 0)
            q_detail[f"{s_key}_mrr"] = s_data["raw_metrics"].get("mrr", 0)
            q_detail[f"{s_key}_first_hit_rank"] = s_data["raw_metrics"].get("first_hit_rank")
            q_chunks[s_key] = s_data.get("top_chunks", [])[:10]

        question_details.append(q_detail)
        chunk_details.append(q_chunks)

    # Compute a simple recall for the DB columns
    n_q = len(results["per_question"])
    raw_recall = results["aggregate"].get("de", {}).get("raw_recall@10")

    tables_used = []
    for s_key in config.get("strategies", []):
        s = STRATEGY_MAP.get(s_key)
        if s:
            tables_used.extend([t.table for t in s.tables])

    insert_sql = """
        INSERT INTO retrieval_eval_runs (
            run_name, goldset_name, table_preset, tables_used, config,
            n_questions, total_elapsed_s,
            raw_distribution, reranked_distribution, selected_distribution,
            n_questions_with_gold, raw_recall, reranked_recall, selected_recall,
            question_details, chunk_details
        ) VALUES (
            %s, %s, %s, %s::jsonb, %s::jsonb,
            %s, %s,
            %s::jsonb, NULL, NULL,
            %s, %s, NULL, NULL,
            %s::jsonb, %s::jsonb
        ) RETURNING id
    """

    insert_params = (
        run_name,
        config.get("goldset_key", "common_corpus"),
        "chunking_eval",
        json.dumps(tables_used),
        json.dumps(config),
        n_q,
        config.get("elapsed_s"),
        json.dumps(results.get("aggregate", {})),
        n_q,
        raw_recall,
        json.dumps(question_details, ensure_ascii=False),
        json.dumps(chunk_details, ensure_ascii=False),
    )

    st.session_state.pop("_chunk_eval_conn", None)

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 * attempt)
                st.session_state.pop("_chunk_eval_conn", None)
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(insert_sql, insert_params)
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("INSERT did not return an id")
                return row["id"]
        except Exception as e:
            st.session_state.pop("_chunk_eval_conn", None)
            if attempt == 2:
                st.warning(f"Erreur sauvegarde DB: {type(e).__name__}: {e}")
                return None


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD SAVED RUNS
# ═══════════════════════════════════════════════════════════════════════════════


def list_saved_runs() -> List[Dict]:
    """Return a list of saved chunking evaluation runs from the DB."""
    conn = get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, run_name, n_questions, total_elapsed_s,
                       created_at, raw_recall
                FROM retrieval_eval_runs
                WHERE table_preset = 'chunking_eval'
                ORDER BY id DESC
                LIMIT 50
            """)
            return cur.fetchall()
    except Exception:
        return []


def load_eval_from_db(run_id: int) -> Optional[Dict]:
    """Reconstruct a results dict from a saved DB run for display."""
    conn = get_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT config, raw_distribution, question_details,
                       n_questions, total_elapsed_s, run_name
                FROM retrieval_eval_runs
                WHERE id = %s
            """, (run_id,))
            row = cur.fetchone()
            if not row:
                return None

        config = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"] or "{}")
        aggregate = row["raw_distribution"] if isinstance(row["raw_distribution"], dict) else json.loads(row["raw_distribution"] or "{}")
        q_details_raw = row["question_details"] if isinstance(row["question_details"], list) else json.loads(row["question_details"] or "[]")

        config["elapsed_s"] = row["total_elapsed_s"]
        config["n_questions"] = row["n_questions"]

        per_question = []
        strategies = config.get("strategies", [])
        for q in q_details_raw:
            entry = {
                "question_id": q.get("question_id"),
                "question": q.get("question", ""),
                "gold_sources": q.get("gold_sources"),
                "gold_chunk_id": q.get("gold_chunk_id"),
                "theme": q.get("theme"),
            }
            for s_key in strategies:
                recall_val = q.get(f"{s_key}_recall@10", 0)
                mrr_val = q.get(f"{s_key}_mrr", 0)
                first_hit = q.get(f"{s_key}_first_hit_rank")
                entry[s_key] = {
                    "raw_metrics": {
                        "recall@10": recall_val,
                        "mrr": mrr_val,
                        "first_hit_rank": first_hit,
                    },
                    "top_chunks": [],
                }
            per_question.append(entry)

        return {
            "config": config,
            "aggregate": aggregate,
            "per_question": per_question,
            "run_name": row["run_name"],
        }
    except Exception as e:
        st.warning(f"Erreur chargement run #{run_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def format_pct(value: float) -> str:
    """Format a ratio as percentage string."""
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def build_summary_table(results: Dict, stage: str = "raw") -> pd.DataFrame:
    """Build a summary DataFrame with all metrics for all strategies."""
    rows = []
    for s in ALL_STRATEGIES:
        if s.key not in results.get("aggregate", {}):
            continue
        agg = results["aggregate"][s.key]
        prefix = f"{stage}_"
        row = {
            "Stratégie": s.name,
        }
        for k in K_VALUES:
            row[f"Recall@{k}"] = format_pct(agg.get(f"{prefix}recall@{k}", 0))
        mrr_val = agg.get(f'{prefix}mrr', 0)
        row["MRR"] = f"{mrr_val:.4f}" if mrr_val is not None else "—"
        for k in K_VALUES:
            ndcg_val = agg.get(f'{prefix}ndcg@{k}', 0)
            row[f"NDCG@{k}"] = f"{ndcg_val:.4f}" if ndcg_val is not None else "—"
        avg_rank = agg.get(f"{prefix}avg_first_hit_rank")
        row["Avg rank 1er hit"] = f"{avg_rank:.2f}" if avg_rank is not None else "—"
        n_found = agg.get(f'{prefix}n_found', 0) or 0
        n_missed = agg.get(f'{prefix}n_missed', 0) or 0
        n_unmapped = agg.get('n_unmapped', 0)
        found_str = f"{n_found} / {n_missed}"
        if n_unmapped > 0:
            found_str += f" ({n_unmapped} non-mappés)"
        row["Trouvés / Manqués"] = found_str
        latency = agg.get(f'{prefix}avg_latency_ms', 0)
        row["Latence moy. (ms)"] = f"{latency:.0f}" if latency is not None else "—"
        rows.append(row)
    return pd.DataFrame(rows)


def recall_curve_chart(results: Dict, stage: str = "raw") -> go.Figure:
    """Line chart: Recall@K vs K for each strategy."""
    fig = go.Figure()
    prefix = f"{stage}_"

    for s in ALL_STRATEGIES:
        if s.key not in results.get("aggregate", {}):
            continue
        agg = results["aggregate"][s.key]
        ys = [agg.get(f"{prefix}recall@{k}", 0) * 100 for k in K_VALUES]
        fig.add_trace(go.Scatter(
            x=K_VALUES,
            y=ys,
            mode="lines+markers",
            name=s.name,
            line=dict(color=s.color, width=3),
            marker=dict(size=10),
        ))

    fig.update_layout(
        title=f"Recall@K — {stage.upper()}",
        xaxis_title="K",
        yaxis_title="Recall (%)",
        yaxis=dict(range=[0, 105]),
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def mrr_comparison_chart(results: Dict, stage: str = "raw") -> go.Figure:
    """Bar chart comparing MRR across strategies."""
    prefix = f"{stage}_"
    names = []
    values = []
    colors = []

    for s in ALL_STRATEGIES:
        if s.key not in results.get("aggregate", {}):
            continue
        agg = results["aggregate"][s.key]
        names.append(s.name)
        values.append(agg.get(f"{prefix}mrr", 0) or 0)
        colors.append(s.color)

    fig = go.Figure(data=[go.Bar(
        x=names,
        y=values,
        marker_color=colors,
        text=[f"{v:.4f}" for v in values],
        textposition="auto",
    )])
    fig.update_layout(
        title=f"MRR — {stage.upper()}",
        yaxis_title="MRR",
        yaxis=dict(range=[0, 1.05]),
        height=350,
    )
    return fig


def first_hit_rank_histogram(results: Dict) -> go.Figure:
    """Histogram of first hit rank per strategy."""
    fig = go.Figure()

    for s in ALL_STRATEGIES:
        ranks = []
        for q in results.get("per_question", []):
            if s.key in q:
                rank = q[s.key]["raw_metrics"].get("first_hit_rank")
                if rank is not None:
                    ranks.append(rank)
        if ranks:
            fig.add_trace(go.Histogram(
                x=ranks,
                name=s.name,
                marker_color=s.color,
                opacity=0.7,
                nbinsx=20,
            ))

    fig.update_layout(
        title="Distribution du rang du 1er hit (raw)",
        xaxis_title="Rang",
        yaxis_title="Nombre de questions",
        barmode="overlay",
        height=400,
    )
    return fig


def per_question_comparison_df(results: Dict, stage: str = "raw") -> pd.DataFrame:
    """Build a per-question comparison DataFrame."""
    rows = []
    strategies = [s for s in ALL_STRATEGIES if s.key in results.get("aggregate", {})]

    for q in results.get("per_question", []):
        row = {
            "question_id": q["question_id"],
            "question": q["question"][:80],
            "gold_sources": q.get("gold_sources", ""),
            "gold_chunk_id": q.get("gold_chunk_id", ""),
            "theme": q.get("theme", ""),
        }

        for s in strategies:
            if s.key not in q:
                continue
            # Skip unmapped questions (V3 or DE)
            if q[s.key].get("v3_unmapped") or q[s.key].get("de_unmapped"):
                row[f"{s.name} hit@10"] = "⬜"  # unmapped
                row[f"{s.name} rank"] = "n/a"
                row[f"{s.name} MRR"] = "n/a"
                continue
            if stage == "rerank" and q[s.key].get("rerank_metrics"):
                metrics = q[s.key]["rerank_metrics"]
            else:
                metrics = q[s.key]["raw_metrics"]
            mrr_val = metrics.get('mrr', 0)
            row[f"{s.name} hit@10"] = "✅" if metrics.get("recall@10", 0) == 1 else "❌"
            row[f"{s.name} rank"] = metrics.get("first_hit_rank", "—")
            row[f"{s.name} MRR"] = f"{mrr_val:.3f}" if mrr_val is not None else "n/a"

        # Classify outcome dynamically
        hits = {}
        for s in strategies:
            if s.key in q:
                if q[s.key].get("v3_unmapped") or q[s.key].get("de_unmapped"):
                    continue  # exclude unmapped from outcome
                if stage == "rerank" and q[s.key].get("rerank_metrics"):
                    m = q[s.key]["rerank_metrics"]
                else:
                    m = q[s.key]["raw_metrics"]
                hits[s.key] = m.get("recall@10", 0) == 1

        n_hits = sum(hits.values())
        if n_hits == len(hits):
            row["outcome"] = "All hit"
        elif n_hits == 0:
            row["outcome"] = "All miss"
        else:
            winners = [STRATEGY_MAP[k].name for k, v in hits.items() if v]
            row["outcome"] = ", ".join(winners)

        rows.append(row)

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: RUN EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def tab_run_evaluation():
    """Configuration and execution of the evaluation."""
    st.header("🚀 Lancer l'évaluation")

    # ── Sidebar config ──
    with st.sidebar:
        st.subheader("Goldset")
        goldset_key = st.selectbox(
            "Jeu de questions",
            options=list(GOLDSETS.keys()),
            format_func=lambda k: GOLDSETS[k].name,
            help="Choisir le goldset pour l'évaluation. Doc-level = match par document, Chunk-level = match par chunk exact."
        )
        gc = GOLDSETS[goldset_key]
        st.caption(f"_{gc.description}_  \nMatch: **{gc.match_mode}**")

        # V3 confidence filter (only for chunk-level goldsets)
        v3_confidence_filter = None
        exclude_errors = False
        require_both = False
        if gc.match_mode == "chunk":
            v3_confidence_filter = st.multiselect(
                "Confiance mapping V3",
                ["high", "medium", "low"],
                default=["high"],
                help="Filtrer les questions par confiance du mapping V3. 'high' = score >= 0.85",
            )
            exclude_errors = st.checkbox("Exclure erreurs de mapping", value=True)
            require_both = st.checkbox(
                "Exiger mapping V1 ET V3",
                value=(goldset_key == "common_corpus_chunk"),  # ON par défaut pour le goldset neutre
                help="Ne garder que les questions ayant un gold_chunk_id pour V1 ET V3. "
                     "Indispensable pour une comparaison équitable au chunk-level.",
            )

        st.divider()
        st.subheader("Configuration")
        publisher_choice = st.selectbox(
            "Source des questions",
            PUBLISHER_OPTIONS,
            index=0,
            help="Filtrer les questions et le retrieval par source. 'Service-Public' = fiches SP uniquement, 'MATTE' = fiches MATTE uniquement.",
        )
        publisher_filter = None if publisher_choice == "Tous" else publisher_choice
        top_k = st.slider("Top K retrieval", 10, 100, 50, step=10)
        max_q = st.number_input("Max questions (0 = toutes)", 0, 500, 0)
        filter_corpus = st.toggle("Filtrer au corpus du goldset", value=True,
                                   help="ON = recherche uniquement dans les documents du goldset. OFF = recherche dans tout le corpus.")
        st.divider()
        enable_rerank = st.checkbox("Activer le reranker (Albert)", value=False)
        rerank_top_k = st.slider("Rerank top K", 5, 50, 20, disabled=not enable_rerank)
        st.divider()
        enable_section_expansion = st.checkbox(
            "Section expansion + rerank",
            value=False,
            help="Expand les chunks en sections (via section_id), deduplique par section, "
                 "puis reranke sur section_markdown. Fonctionne pour DE Mapped, Hybrid, et V3.",
        )
        section_rerank_top_k = st.slider(
            "Section rerank top K", 5, 30, 15,
            disabled=not enable_section_expansion,
        )

    # ── Strategy selection ──
    st.subheader("Stratégies à comparer")

    # Strategy selector
    selected_keys = st.multiselect(
        "Sélectionner les stratégies",
        options=[s.key for s in ALL_STRATEGIES],
        default=[s.key for s in ALL_STRATEGIES],
        format_func=lambda k: STRATEGY_MAP[k].name,
    )
    selected_strategies = [STRATEGY_MAP[k] for k in selected_keys]

    if not selected_strategies:
        st.warning("Sélectionnez au moins une stratégie.")
        return

    # Display selected strategies
    cols = st.columns(len(selected_strategies))
    color_icons = {"de": "🔴", "de_ctx": "🟠", "v3": "🟢", "v3_raw": "🟡", "v3_raw_text": "🔵", "v2_original": "🟣", "hf_sp": "🟤", "de_mapped": "🔻", "hybrid": "🟪"}
    for col, s in zip(cols, selected_strategies):
        with col:
            icon = color_icons.get(s.key, "⚪")
            st.markdown(f"**{icon} {s.name}**")
            st.caption(s.description)
            tables_str = ", ".join([t.table for t in s.tables])
            embed_str = s.tables[0].embed_col if s.tables else "?"
            st.code(f"Tables: {tables_str}\nEmbed: {embed_str}", language=None)

    # ── Load a saved run ──
    st.divider()
    saved_runs = list_saved_runs()
    if saved_runs:
        run_options = {r["id"]: f"#{r['id']} — {r['run_name']} ({r['n_questions']}q, {r.get('total_elapsed_s', 0):.0f}s)" for r in saved_runs}
        selected_run_id = st.selectbox(
            "Charger un run précédent",
            options=[None] + list(run_options.keys()),
            format_func=lambda x: "— Nouveau run —" if x is None else run_options[x],
        )
        if selected_run_id and st.button("📂 Charger ce run"):
            loaded = load_eval_from_db(selected_run_id)
            if loaded:
                st.session_state["chunk_eval_results"] = loaded
                st.session_state["chunk_eval_run_name"] = loaded.get("run_name", "")
                st.success(f"Run #{selected_run_id} chargé")
                st.rerun()

    # ── Run button ──
    st.divider()
    strats_short = "+".join(selected_keys)
    conf_str = f"_conf{''.join(c[0] for c in v3_confidence_filter)}" if v3_confidence_filter else ""
    pub_str = f"_{publisher_choice.lower().replace('-','')}" if publisher_filter else ""
    both_str = "_both" if (gc.match_mode == "chunk" and require_both) else ""
    sec_str = f"_secexp{section_rerank_top_k}" if enable_section_expansion else ""
    auto_name = f"chunking_eval_{goldset_key}_{strats_short}_{'filtered' if filter_corpus else 'full'}_k{top_k}{pub_str}{conf_str}{both_str}{'_rr' + str(rerank_top_k) if enable_rerank else ''}{sec_str}"
    run_name = st.text_input("Nom du run", value=auto_name)

    if st.button("Lancer l'évaluation", type="primary", width="stretch"):
        eval_results = run_evaluation(
            strategies=selected_strategies,
            goldset_key=goldset_key,
            top_k=top_k,
            max_questions=max_q if max_q > 0 else None,
            enable_rerank=enable_rerank,
            rerank_top_k=rerank_top_k,
            publisher_filter=publisher_filter,
            filter_corpus=filter_corpus,
            v3_confidence_filter=v3_confidence_filter,
            exclude_mapping_errors=exclude_errors if gc.match_mode == "chunk" else False,
            require_both_mappings=require_both if gc.match_mode == "chunk" else False,
            enable_section_expansion=enable_section_expansion,
            section_rerank_top_k=section_rerank_top_k,
        )
        if eval_results:
            st.session_state["chunk_eval_results"] = eval_results
            st.session_state["chunk_eval_run_name"] = run_name

            # Auto-save
            run_id = save_eval_to_db(eval_results, run_name)
            if run_id:
                st.success(f"💾 Run sauvegardé (id={run_id})")

    # Show results if already computed
    if "chunk_eval_results" in st.session_state:
        _display_summary_metrics()


def _display_summary_metrics():
    """Quick summary of results on Tab 1."""
    results = st.session_state.get("chunk_eval_results", {})
    if not results:
        return

    st.divider()
    st.subheader("📊 Résultats rapides")

    config = results.get("config", {})
    st.caption(
        f"Top K: {config.get('top_k')} | "
        f"Questions: {config.get('n_questions')} | "
        f"Corpus filter: {'ON' if config.get('filter_corpus') else 'OFF'} | "
        f"Durée: {config.get('elapsed_s', 0):.1f}s"
    )

    df = build_summary_table(results, stage="raw")
    st.dataframe(df, width="stretch", hide_index=True)

    if config.get("enable_rerank"):
        st.caption("**Après reranking:**")
        df_rr = build_summary_table(results, stage="rerank")
        st.dataframe(df_rr, width="stretch", hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: RESULTS COMPARISON
# ═══════════════════════════════════════════════════════════════════════════════

def tab_results_comparison():
    """Detailed charts and analysis of results."""
    st.header("📊 Comparaison des résultats")

    results = st.session_state.get("chunk_eval_results")
    if not results:
        st.info("Lancez d'abord une évaluation dans l'onglet 'Run Evaluation'.")
        return

    config = results.get("config", {})
    stage = "raw"

    if config.get("enable_rerank"):
        stage = st.radio("Étape", ["raw", "rerank"], horizontal=True)

    # ── Summary table ──
    st.subheader("Tableau récapitulatif")
    df = build_summary_table(results, stage=stage)
    st.dataframe(df, width="stretch", hide_index=True)

    # ── Charts ──
    col1, col2 = st.columns(2)

    with col1:
        fig = recall_curve_chart(results, stage=stage)
        st.plotly_chart(fig, width="stretch")

    with col2:
        fig = mrr_comparison_chart(results, stage=stage)
        st.plotly_chart(fig, width="stretch")

    # ── First hit rank distribution ──
    st.subheader("Distribution du rang du 1er hit")
    fig = first_hit_rank_histogram(results)
    st.plotly_chart(fig, width="stretch")

    # ── Outcome breakdown ──
    st.subheader("Répartition des résultats (Recall@10)")
    per_q_df = per_question_comparison_df(results, stage=stage)
    if not per_q_df.empty and "outcome" in per_q_df.columns:
        outcome_counts = per_q_df["outcome"].value_counts().sort_index()

        # Dynamic colors
        base_colors = {"All hit": "#4CAF50", "All miss": "#9E9E9E"}
        for s in ALL_STRATEGIES:
            base_colors[s.name] = s.color

        # Display metrics
        n_outcomes = len(outcome_counts)
        cols = st.columns(min(n_outcomes, 6))
        for col, (label, count) in zip(cols, outcome_counts.items()):
            pct = count / len(per_q_df) * 100 if len(per_q_df) > 0 else 0
            col.metric(label, f"{count}", f"{pct:.1f}%")

        # Pie chart
        fig = go.Figure(data=[go.Pie(
            labels=list(outcome_counts.index),
            values=list(outcome_counts.values),
            marker_colors=[base_colors.get(l, "#ccc") for l in outcome_counts.index],
            hole=0.3,
        )])
        fig.update_layout(title="Répartition Recall@10", height=350)
        st.plotly_chart(fig, width="stretch")

    # ── NDCG comparison ──
    st.subheader("NDCG@K comparaison")
    prefix = f"{stage}_"
    ndcg_data = []
    for s in ALL_STRATEGIES:
        if s.key not in results.get("aggregate", {}):
            continue
        agg = results["aggregate"][s.key]
        for k in K_VALUES:
            ndcg_data.append({
                "Strategy": s.name,
                "K": k,
                "NDCG": agg.get(f"{prefix}ndcg@{k}", 0) or 0,
            })
    if ndcg_data:
        ndcg_df = pd.DataFrame(ndcg_data)
        fig = px.bar(
            ndcg_df, x="K", y="NDCG", color="Strategy",
            barmode="group",
            color_discrete_map={s.name: s.color for s in ALL_STRATEGIES},
            title=f"NDCG@K — {stage.upper()}",
        )
        fig.update_layout(height=400, yaxis=dict(range=[0, 1.05]))
        st.plotly_chart(fig, width="stretch")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: PER-QUESTION DETAIL
# ═══════════════════════════════════════════════════════════════════════════════

def tab_per_question_detail():
    """Per-question analysis with error breakdown."""
    st.header("🔎 Détail par question")

    results = st.session_state.get("chunk_eval_results")
    if not results:
        st.info("Lancez d'abord une évaluation dans l'onglet 'Run Evaluation'.")
        return

    per_q_df = per_question_comparison_df(results)
    if per_q_df.empty:
        st.warning("Aucun résultat disponible.")
        return

    # ── Filters ──
    col1, col2 = st.columns(2)
    with col1:
        all_outcomes = sorted(per_q_df["outcome"].unique().tolist())
        outcome_filter = st.multiselect(
            "Filtrer par résultat",
            all_outcomes,
            default=all_outcomes,
        )
    with col2:
        themes = sorted(per_q_df["theme"].dropna().unique().tolist())
        theme_filter = st.multiselect("Filtrer par thème", themes, default=themes)

    filtered = per_q_df[
        per_q_df["outcome"].isin(outcome_filter)
        & per_q_df["theme"].isin(theme_filter)
    ]

    st.info(f"**{len(filtered)}** questions affichées sur {len(per_q_df)}")

    # ── Table ──
    st.dataframe(
        filtered,
        width="stretch",
        height=500,
        column_config={
            "question_id": st.column_config.NumberColumn("ID", width="small"),
            "question": st.column_config.TextColumn("Question", width="large"),
            "gold_sources": st.column_config.TextColumn("Gold Source", width="medium"),
        },
    )

    # ── Detail expander for a selected question ──
    st.divider()
    st.subheader("Détail d'une question")

    question_ids = filtered["question_id"].tolist()
    if not question_ids:
        return

    selected_qid = st.selectbox("Question ID", question_ids)

    q_data = next((q for q in results["per_question"] if q["question_id"] == selected_qid), None)
    if not q_data:
        return

    st.markdown(f"**Question:** {q_data['question']}")
    st.markdown(f"**Gold source:** `{q_data.get('gold_sources', '—')}`")
    if q_data.get("gold_chunk_id"):
        st.markdown(f"**Gold chunk ID (V1):** `{q_data['gold_chunk_id']}`")
    if q_data.get("gold_chunk_id_v3"):
        st.markdown(f"**Gold chunk ID (V3):** `{q_data['gold_chunk_id_v3']}`")

    strategies = [s for s in ALL_STRATEGIES if s.key in q_data]
    tabs = st.tabs([s.name for s in strategies])

    for tab, s in zip(tabs, strategies):
        with tab:
            s_data = q_data[s.key]

            # Handle unmapped questions
            if s_data.get("v3_unmapped") or s_data.get("de_unmapped"):
                unmapped_type = "V3" if s_data.get("v3_unmapped") else "V1"
                st.warning(f"⬜ Question non-mappée ({unmapped_type}) — pas de gold_chunk_id pour cette stratégie.")
                continue

            metrics = s_data["raw_metrics"]

            # Metrics summary
            mcol1, mcol2, mcol3, mcol4 = st.columns(4)
            mcol1.metric("Recall@10", "✅" if metrics.get("recall@10") == 1 else "❌")
            mrr_val = metrics.get('mrr', 0)
            mcol2.metric("MRR", f"{mrr_val:.4f}" if mrr_val is not None else "—")
            mcol3.metric("1er hit rang", metrics.get("first_hit_rank", "—"))
            mcol4.metric("Latence", f"{s_data.get('retrieval_time_ms', 0)} ms")

            # Top chunks
            st.caption("**Top 20 chunks récupérés:**")
            chunks_df = pd.DataFrame(s_data.get("top_chunks", []))
            if not chunks_df.empty:
                st.dataframe(
                    chunks_df,
                    width="stretch",
                    column_config={
                        "is_hit": st.column_config.CheckboxColumn("Hit?"),
                        "score": st.column_config.NumberColumn("Score", format="%.5f"),
                    },
                )

            # Reranked chunks
            if s_data.get("top_reranked"):
                st.caption("**Après reranking:**")
                rr_df = pd.DataFrame(s_data["top_reranked"])
                st.dataframe(rr_df, width="stretch")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: UNION RETRIEVAL (HEAD-TO-HEAD)
# ═══════════════════════════════════════════════════════════════════════════════

def run_union_retrieval(
    cur,
    strategies: List[ChunkingStrategy],
    query_embedding: List[float],
    top_k: int = 20,
    filter_short_ids: Optional[List[str]] = None,
    publisher_filter: Optional[str] = None,
) -> List[Dict]:
    """
    Run retrieval on ALL strategies and merge into a single ranked list.
    Each chunk is tagged with its strategy key. Sorted by cosine score desc.
    publisher_filter: 'MATTE', 'Service-Public', or None for all.
    """
    all_chunks = []

    for strategy in strategies:
        is_v3 = any(t.short_id_mode == "join_documents" for t in strategy.tables)

        if is_v3:
            embed_col = strategy.tables[0].embed_col if strategy.tables else "embedding"
            chunks = search_v3_table(
                cur, query_embedding, top_k=top_k,
                filter_short_ids=filter_short_ids,
                embed_col=embed_col,
                strategy_key=strategy.key,
                publisher_filter=publisher_filter,
            )
            for c in chunks:
                c["strategy"] = strategy.key
            all_chunks.extend(chunks)
        else:
            for table_cfg in strategy.tables:
                # Skip DE tables not matching publisher filter
                if publisher_filter:
                    table_pub = DE_TABLE_PUBLISHER.get(table_cfg.table)
                    if table_pub and table_pub != publisher_filter:
                        continue
                chunks = search_de_table(
                    cur, table_cfg, query_embedding,
                    top_k=top_k,
                    filter_short_ids=filter_short_ids,
                )
                for c in chunks:
                    c["strategy"] = strategy.key
                all_chunks.extend(chunks)

    # Sort all chunks by score desc
    all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
    return all_chunks


def tab_union_retrieval():
    """Head-to-head union retrieval across all chunking strategies."""
    st.header("Union Retrieval (Head-to-Head)")
    st.markdown(
        "Toutes les stratégies en compétition sur la même query. "
        "Les chunks de toutes les tables sont triés par score cosinus dans un seul classement."
    )

    # ── Config ──
    with st.sidebar:
        st.divider()
        st.subheader("Union Retrieval")
        union_goldset = st.selectbox(
            "Goldset (union)",
            options=list(GOLDSETS.keys()),
            format_func=lambda k: GOLDSETS[k].name,
            key="union_goldset",
        )
        union_publisher = st.selectbox(
            "Source (union)",
            PUBLISHER_OPTIONS,
            index=0,
            key="union_publisher",
            help="Filtrer questions et retrieval par source.",
        )
        union_pub_filter = None if union_publisher == "Tous" else union_publisher
        union_top_k = st.slider("Top K par stratégie", 5, 50, 20, key="union_top_k")
        union_max_q = st.number_input("Max questions (0 = toutes)", 0, 500, 0, key="union_max_q")
        union_filter = st.toggle("Filtrer au corpus", value=True, key="union_filter")

    # ── Strategy selection ──
    selected_keys = st.multiselect(
        "Stratégies en compétition",
        options=[s.key for s in ALL_STRATEGIES],
        default=[s.key for s in ALL_STRATEGIES],
        format_func=lambda k: STRATEGY_MAP[k].name,
        key="union_strategies",
    )
    selected_strategies = [STRATEGY_MAP[k] for k in selected_keys]

    if len(selected_strategies) < 2:
        st.warning("Sélectionnez au moins 2 stratégies.")
        return

    if st.button("Lancer le tournoi", type="primary", width="stretch"):
        questions_df = load_goldset_questions(union_goldset, limit=union_max_q if union_max_q > 0 else None)
        if questions_df.empty:
            st.error("Aucune question trouvée.")
            return

        filter_short_ids = load_goldset_short_ids(union_goldset) if union_filter else None

        # Publisher filter: filter questions and short_ids
        if union_pub_filter:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT short_id FROM rag_documents WHERE publisher = %s",
                    [union_pub_filter]
                )
                pub_short_ids = set(r["short_id"] for r in cur.fetchall())
            # Filter questions
            questions_df = questions_df[questions_df["gold_sources"].apply(lambda value: gold_sources_overlap(value, pub_short_ids))]
            # Filter short_ids
            if filter_short_ids:
                filter_short_ids = [sid for sid in filter_short_ids if sid in pub_short_ids]

        if questions_df.empty:
            st.error("Aucune question trouvée pour cette source.")
            return

        pub_label = union_pub_filter or "Tous"
        st.info(f"**{len(questions_df)}** questions | Source: **{pub_label}** | {len(selected_strategies)} stratégies | Top K: {union_top_k}")

        progress = st.progress(0, text="Lancement du tournoi...")
        conn = get_connection()

        per_question = []

        with conn.cursor() as cur:
            for i, (_, row) in enumerate(questions_df.iterrows()):
                embedding = row["embedding_albert"]
                if embedding is None:
                    continue

                gold_sources = row.get("gold_sources", "")

                progress.progress(
                    (i + 1) / len(questions_df),
                    text=f"Question {i+1}/{len(questions_df)}: {row['question'][:50]}..."
                )

                # Union retrieval
                all_chunks = run_union_retrieval(
                    cur, selected_strategies, embedding,
                    top_k=union_top_k,
                    filter_short_ids=filter_short_ids,
                    publisher_filter=union_pub_filter,
                )

                # Find best gold-doc chunk per strategy
                strategy_best = {}  # strategy_key -> {rank, score, chunk_id}
                for rank, chunk in enumerate(all_chunks, 1):
                    s_key = chunk.get("strategy", "?")
                    chunk_short_id = chunk.get("short_id", "")

                    is_gold = gold_sources_match(gold_sources, chunk_short_id)

                    if is_gold and s_key not in strategy_best:
                        strategy_best[s_key] = {
                            "rank": rank,
                            "score": round(chunk.get("score", 0), 5),
                            "chunk_id": chunk.get("chunk_id", ""),
                        }

                # Who wins? (lowest rank for gold doc)
                winner = None
                if strategy_best:
                    winner = min(strategy_best, key=lambda k: strategy_best[k]["rank"])

                per_question.append({
                    "question_id": row["id"],
                    "question": row["question"],
                    "gold_sources": format_gold_sources(gold_sources),
                    "winner": winner,
                    "strategy_best": strategy_best,
                    "top_10": [
                        {
                            "rank": r + 1,
                            "strategy": c.get("strategy", "?"),
                            "chunk_id": c.get("chunk_id", "")[:12],
                            "short_id": c.get("short_id", ""),
                            "score": round(c.get("score", 0), 5),
                            "is_gold": gold_sources_match(gold_sources, c.get("short_id", "")),
                            "source_name": c.get("source_name", "")[:50],
                        }
                        for r, c in enumerate(all_chunks[:10])
                    ],
                })

        progress.progress(1.0, text="Tournoi terminé!")
        st.session_state["union_results"] = per_question

    # ── Display results ──
    per_question = st.session_state.get("union_results")
    if not per_question:
        st.info("Lance le tournoi pour voir les résultats.")
        return

    n = len(per_question)

    # ── Aggregate stats ──
    st.subheader(f"Résultats ({n} questions)")

    # Win count
    win_counts = {}
    for q in per_question:
        w = q.get("winner")
        if w:
            win_counts[w] = win_counts.get(w, 0) + 1

    no_winner = sum(1 for q in per_question if not q.get("winner"))

    # Strategy presence in top-1 (regardless of gold)
    top1_counts = {}
    for q in per_question:
        if q["top_10"]:
            s = q["top_10"][0]["strategy"]
            top1_counts[s] = top1_counts.get(s, 0) + 1

    # Avg best rank and score per strategy (when found)
    avg_ranks = {}
    avg_scores = {}
    for q in per_question:
        for s_key, info in q.get("strategy_best", {}).items():
            if s_key not in avg_ranks:
                avg_ranks[s_key] = []
                avg_scores[s_key] = []
            avg_ranks[s_key].append(info["rank"])
            avg_scores[s_key].append(info["score"])

    # Display
    st.markdown("### Qui trouve le gold document en premier ?")

    summary_rows = []
    for s in ALL_STRATEGIES:
        if s.key not in selected_keys:
            continue
        wins = win_counts.get(s.key, 0)
        top1 = top1_counts.get(s.key, 0)
        ranks = avg_ranks.get(s.key, [])
        scores = avg_scores.get(s.key, [])
        summary_rows.append({
            "Stratégie": s.name,
            "Wins (gold #1)": wins,
            "Win %": f"{100*wins/max(n,1):.1f}%",
            "Top-1 (tous)": top1,
            "Top-1 %": f"{100*top1/max(n,1):.1f}%",
            "Avg rank gold": f"{np.mean(ranks):.2f}" if ranks else "—",
            "Median rank": f"{np.median(ranks):.1f}" if ranks else "—",
            "Avg score gold": f"{np.mean(scores):.4f}" if scores else "—",
            "Found": f"{len(ranks)}/{n}",
        })

    if no_winner:
        summary_rows.append({
            "Stratégie": "Aucun (miss)",
            "Wins (gold #1)": no_winner,
            "Win %": f"{100*no_winner/max(n,1):.1f}%",
            "Top-1 (tous)": "",
            "Top-1 %": "",
            "Avg rank gold": "",
            "Found": "",
        })

    st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

    # ── Win distribution chart ──
    st.markdown("### Distribution des victoires")
    win_data = []
    for s in ALL_STRATEGIES:
        if s.key in selected_keys and s.key in win_counts:
            win_data.append({"Stratégie": s.name, "Wins": win_counts[s.key], "color": s.color})

    if win_data:
        fig = go.Figure(data=[
            go.Bar(
                x=[d["Stratégie"] for d in win_data],
                y=[d["Wins"] for d in win_data],
                marker_color=[d["color"] for d in win_data],
                text=[d["Wins"] for d in win_data],
                textposition="auto",
            )
        ])
        fig.update_layout(
            title=f"Nombre de fois où chaque stratégie place le gold chunk en premier ({n} questions)",
            yaxis_title="Nombre de victoires",
            height=400,
        )
        st.plotly_chart(fig, width="stretch")

    # ── Top-10 strategy distribution ──
    st.markdown("### Distribution des stratégies dans le Top-10")
    top10_dist = {}
    for q in per_question:
        for chunk in q["top_10"]:
            s = chunk["strategy"]
            if s not in top10_dist:
                top10_dist[s] = {r: 0 for r in range(1, 11)}
            top10_dist[s][chunk["rank"]] = top10_dist[s].get(chunk["rank"], 0) + 1

    if top10_dist:
        fig2 = go.Figure()
        for s in ALL_STRATEGIES:
            if s.key in top10_dist:
                ranks = list(range(1, 11))
                counts = [top10_dist[s.key].get(r, 0) for r in ranks]
                fig2.add_trace(go.Bar(
                    x=ranks,
                    y=counts,
                    name=s.name,
                    marker_color=s.color,
                ))
        fig2.update_layout(
            title="Quelle stratégie occupe chaque rang du Top 10 ?",
            xaxis_title="Rang",
            yaxis_title="Nombre de questions",
            barmode="stack",
            height=400,
        )
        st.plotly_chart(fig2, width="stretch")

    # ── Score distribution of gold chunks ──
    st.markdown("### Distribution des scores gold par stratégie")
    score_traces = []
    for s in ALL_STRATEGIES:
        if s.key in selected_keys and s.key in avg_scores:
            score_traces.append(go.Box(
                y=avg_scores[s.key],
                name=s.name,
                marker_color=s.color,
                boxmean=True,
            ))
    if score_traces:
        fig3 = go.Figure(data=score_traces)
        fig3.update_layout(
            title="Score cosinus du meilleur gold chunk par stratégie",
            yaxis_title="Score cosinus",
            height=400,
        )
        st.plotly_chart(fig3, width="stretch")

    # ── Breakdown par document (short_id) ──
    st.divider()
    st.markdown("### Breakdown par document")
    st.caption("Quelle stratégie gagne le plus souvent, document par document ?")

    # Build per-document stats
    doc_stats = {}  # short_id -> {strategy_key -> wins, total}
    for q in per_question:
        sid = q.get("gold_sources", "")
        if not sid:
            continue
        if sid not in doc_stats:
            doc_stats[sid] = {"total": 0}
            for s in ALL_STRATEGIES:
                if s.key in selected_keys:
                    doc_stats[sid][s.key] = 0
        doc_stats[sid]["total"] += 1
        w = q.get("winner")
        if w and w in doc_stats[sid]:
            doc_stats[sid][w] += 1

    if doc_stats:
        doc_rows = []
        for sid in sorted(doc_stats.keys()):
            d = doc_stats[sid]
            total = d["total"]
            row = {"Document (short_id)": sid, "Questions": total}
            best_key = None
            best_wins = 0
            for s in ALL_STRATEGIES:
                if s.key in selected_keys:
                    wins = d.get(s.key, 0)
                    row[s.name] = f"{wins}/{total}"
                    if wins > best_wins:
                        best_wins = wins
                        best_key = s.key
            row["Dominant"] = STRATEGY_MAP[best_key].name if best_key else "—"
            doc_rows.append(row)

        doc_df = pd.DataFrame(doc_rows)

        # Summary: how many docs does each strategy dominate?
        dominance = {}
        for row in doc_rows:
            dom = row["Dominant"]
            dominance[dom] = dominance.get(dom, 0) + 1

        cols = st.columns(len(dominance))
        for i, (name, count) in enumerate(sorted(dominance.items(), key=lambda x: -x[1])):
            cols[i].metric(f"{name}", f"{count} docs", f"{100*count/len(doc_stats):.0f}%")

        # Heatmap: win rate per document per strategy
        if len(selected_keys) >= 2:
            heatmap_data = []
            heatmap_sids = []
            for sid in sorted(doc_stats.keys()):
                d = doc_stats[sid]
                total = d["total"]
                if total == 0:
                    continue
                heatmap_sids.append(sid[:25])
                heatmap_data.append([
                    d.get(s.key, 0) / total
                    for s in ALL_STRATEGIES if s.key in selected_keys
                ])

            if heatmap_data and len(heatmap_sids) > 1:
                strat_names = [s.name for s in ALL_STRATEGIES if s.key in selected_keys]
                heatmap_arr = np.array(heatmap_data)
                fig_hm = go.Figure(data=go.Heatmap(
                    z=heatmap_arr.T,
                    x=heatmap_sids,
                    y=strat_names,
                    colorscale="RdYlGn",
                    zmin=0, zmax=1,
                    text=[[f"{v:.0%}" for v in row] for row in heatmap_arr.T],
                    texttemplate="%{text}",
                    hovertemplate="Doc: %{x}<br>Stratégie: %{y}<br>Win rate: %{z:.0%}<extra></extra>",
                ))
                fig_hm.update_layout(
                    title="Win rate par document et stratégie",
                    xaxis_title="Document",
                    height=max(300, 60 * len(strat_names)),
                    xaxis=dict(tickangle=45),
                )
                st.plotly_chart(fig_hm, width="stretch")

        with st.expander("Tableau détaillé par document", expanded=False):
            st.dataframe(doc_df, width="stretch", hide_index=True, height=400)

    # ── Per-question detail ──
    st.divider()
    st.markdown("### Détail par question")

    detail_rows = []
    for q in per_question:
        row = {
            "ID": q["question_id"],
            "Question": q["question"][:70],
            "Gold": q["gold_sources"],
            "Winner": STRATEGY_MAP[q["winner"]].name if q.get("winner") and q["winner"] in STRATEGY_MAP else "—",
        }
        for s in ALL_STRATEGIES:
            if s.key in selected_keys:
                info = q.get("strategy_best", {}).get(s.key)
                row[f"{s.name} rank"] = info["rank"] if info else "—"
                row[f"{s.name} score"] = info["score"] if info else "—"
        detail_rows.append(row)

    detail_df = pd.DataFrame(detail_rows)
    st.dataframe(detail_df, width="stretch", height=400, hide_index=True)

    # ── Single question drill-down ──
    st.divider()
    qids = [q["question_id"] for q in per_question]
    selected_qid = st.selectbox("Voir le Top-10 d'une question", qids, key="union_qid")
    q_data = next((q for q in per_question if q["question_id"] == selected_qid), None)
    if q_data:
        st.markdown(f"**Question:** {q_data['question']}")
        st.markdown(f"**Gold source:** `{q_data['gold_sources']}`")
        top10_df = pd.DataFrame(q_data["top_10"])
        if not top10_df.empty:
            # Color the strategy column
            st.dataframe(
                top10_df,
                width="stretch",
                column_config={
                    "is_gold": st.column_config.CheckboxColumn("Gold?"),
                    "score": st.column_config.NumberColumn("Score", format="%.5f"),
                },
                hide_index=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LAYOUT
# ═══════════════════════════════════════════════════════════════════════════════

tab1, tab2, tab3, tab4 = st.tabs([
    "Run Evaluation",
    "Résultats & Comparaison",
    "Détail par question",
    "Union Retrieval",
])

with tab1:
    tab_run_evaluation()

with tab2:
    tab_results_comparison()

with tab3:
    tab_per_question_detail()

with tab4:
    tab_union_retrieval()
