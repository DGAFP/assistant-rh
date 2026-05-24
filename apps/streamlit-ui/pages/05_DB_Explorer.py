"""
🗄️ DB Explorer - Explore RAG chunks in the database

This page allows you to:
- Browse chunks table by table
- View documents grouped by title with chunk counts
- Inspect individual chunks in order
"""

import html
import unicodedata

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import text

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.db_utils import get_engine


def normalize_text(text: str) -> str:
    """Normalise le texte en retirant les accents pour la recherche."""
    if not text:
        return ""
    # Décompose les caractères accentués, retire les accents, recompose
    normalized = unicodedata.normalize('NFD', text)
    return ''.join(c for c in normalized if unicodedata.category(c) != 'Mn').lower()

load_dotenv()

# ============================================================================
# PAGE CONFIG
# ============================================================================

st.set_page_config(
    page_title="DB Explorer",
    page_icon="🗄️",
    layout="wide"
)

require_admin()
show_admin_badge()

# ============================================================================
# ORIGINAL CONFIG (marker) - Already set above with admin auth
# ============================================================================

st.markdown("""
<style>
:root {
    --blue-france: #003091;
    --grey-950: #161616;
    --grey-200: #E5E5E5;
    --grey-50: #F6F6F6;
}
/* Document row - clickable */
.doc-row {
    border: 1px solid var(--grey-200);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin-bottom: 0.5rem;
    background: white;
    cursor: pointer;
    transition: all 0.2s ease;
}
.doc-row:hover {
    border-color: var(--blue-france);
    background: #f8faff;
}
.doc-row.selected {
    border-color: var(--blue-france);
    border-width: 2px;
    background: #eef2ff;
}
/* Chunk card */
.chunk-card {
    border: 1px solid var(--grey-200);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin-bottom: 0.5rem;
    background: var(--grey-50);
}
.chunk-card .chunk-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--grey-200);
}
.chunk-card .chunk-index {
    font-weight: 700;
    color: var(--blue-france);
    font-size: 14px;
}
.chunk-card .chunk-meta {
    font-size: 12px;
    color: #666;
}
.chunk-card .chunk-text {
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
}
/* Badge */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    margin-left: 8px;
}
.badge-count {
    background: #dbeafe;
    color: #1e40af;
}
.badge-table {
    background: #f0fdf4;
    color: #166534;
}
/* Stats */
.stat-box {
    background: white;
    border: 1px solid var(--grey-200);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
}
.stat-value {
    font-size: 28px;
    font-weight: 700;
    color: var(--blue-france);
}
.stat-label {
    font-size: 13px;
    color: #666;
    margin-top: 4px;
}
</style>
""", unsafe_allow_html=True)

# ============================================================================
# TABLE CONFIGURATION
# ============================================================================

TABLE_CONFIG = {
    "rag_chunks_matte": {
        "display_name": "📋 MATTE",
        "title_column": "source_name",
        "order_column": "chunk_index",
        "text_column": "text",
        "id_column": "hash_id",
        "extra_columns": ["section_path", "thematique", "short_id"],
        "default": True,
    },
    "rag_chunks_service_public": {
        "display_name": "🏛️ Service Public",
        "title_column": "source_name",
        "order_column": "chunk_index",
        "text_column": "text",
        "id_column": "hash_id",
        "extra_columns": ["section_path", "thematique", "short_id"],
        "default": True,
    },
    "rag_chunks_dgafp": {
        "display_name": "📜 DGAFP",
        "title_column": "title",
        "order_column": "number",
        "text_column": "chunk_text",
        "id_column": "cid",
        "extra_columns": ["full_title", "category", "url"],
        "default": True,
    },
    "rag_chunks_rgrh": {
        "display_name": "📚 RGRH",
        "title_column": "source_name",
        "order_column": "chunk_index",
        "text_column": "text",
        "id_column": "hash_id",
        "extra_columns": ["section_path", "thematique"],
        "default": True,
    },
    "rag_chunks_legifrance": {
        "display_name": "⚖️ Legifrance (new)",
        "title_column": "source_name",
        "order_column": "chunk_index",
        "text_column": "text",
        "id_column": "hash_id",
        "extra_columns": ["section_path", "thematique"],
        "default": False,
    },
}

DEFAULT_TABLES = [k for k, v in TABLE_CONFIG.items() if v.get("default")]

get_db_engine = get_engine


# ============================================================================
# DATA LOADING FUNCTIONS
# ============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def get_table_stats(table_name: str) -> dict:
    """Get basic stats for a table."""
    engine = get_db_engine()
    if not engine:
        return {"total_chunks": 0, "unique_titles": 0}

    config = TABLE_CONFIG.get(table_name, {})
    title_col = config.get("title_column", "title")

    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '30s'"))
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
            unique = conn.execute(text(f"SELECT COUNT(DISTINCT {title_col}) FROM {table_name}")).scalar()
            return {"total_chunks": total, "unique_titles": unique}
    except Exception as e:
        st.error(f"Erreur stats: {e}")
        return {"total_chunks": 0, "unique_titles": 0}


def get_multi_table_stats(table_names: list) -> dict:
    """Aggregate stats across multiple tables."""
    total_chunks = 0
    total_docs = 0
    for tbl in table_names:
        stats = get_table_stats(tbl)
        total_chunks += stats["total_chunks"]
        total_docs += stats["unique_titles"]
    return {"total_chunks": total_chunks, "unique_titles": total_docs}


@st.cache_data(ttl=300, show_spinner=False)
def get_documents_list(table_name: str) -> pd.DataFrame:
    """Get list of unique documents with chunk counts for one table."""
    engine = get_db_engine()
    if not engine:
        return pd.DataFrame()

    config = TABLE_CONFIG.get(table_name, {})
    title_col = config.get("title_column", "title")

    try:
        with engine.connect() as conn:
            query = f"""
                SELECT {title_col} as title, COUNT(*) as chunk_count
                FROM {table_name}
                WHERE {title_col} IS NOT NULL
                GROUP BY {title_col}
                ORDER BY {title_col}
            """
            result = conn.execute(text(query))
            return pd.DataFrame(result.fetchall(), columns=result.keys())
    except Exception as e:
        st.error(f"Erreur chargement documents: {e}")
        return pd.DataFrame()


def get_multi_table_documents(table_names: list) -> pd.DataFrame:
    """Get documents across multiple tables with table indicator."""
    all_dfs = []
    for tbl in table_names:
        df = get_documents_list(tbl)
        if not df.empty:
            df["table"] = tbl
            cfg = TABLE_CONFIG[tbl]
            df["table_display"] = cfg["display_name"].split(" ")[0]
            all_dfs.append(df)
    if all_dfs:
        return pd.concat(all_dfs, ignore_index=True)
    return pd.DataFrame()


def natural_sort_key(val):
    """Convert value to a sortable key for natural ordering."""
    import re
    if pd.isna(val):
        return (0, "")
    val_str = str(val)
    match = re.match(r'^(\d+)', val_str)
    if match:
        return (int(match.group(1)), val_str)
    return (0, val_str)


@st.cache_data(ttl=300, show_spinner=False)
def get_chunks_for_document(table_name: str, title: str) -> pd.DataFrame:
    """Get all chunks for a specific document, ordered numerically."""
    engine = get_db_engine()
    if not engine:
        return pd.DataFrame()

    config = TABLE_CONFIG.get(table_name, {})
    title_col = config.get("title_column", "title")
    order_col = config.get("order_column", "id")
    text_col = config.get("text_column", "text")
    id_col = config.get("id_column", "id")
    extra_cols = config.get("extra_columns", [])

    seen = set()
    unique_columns = []
    for col in [id_col, order_col, text_col] + extra_cols:
        if col not in seen:
            seen.add(col)
            unique_columns.append(col)
    columns_str = ", ".join(unique_columns)

    try:
        with engine.connect() as conn:
            query = f"""
                SELECT {columns_str}
                FROM {table_name}
                WHERE {title_col} = :title
            """
            result = conn.execute(text(query), {"title": title})
            df = pd.DataFrame(result.fetchall(), columns=result.keys())

            if not df.empty and order_col in df.columns:
                df["_sort_key"] = df[order_col].apply(natural_sort_key)
                df = df.sort_values("_sort_key").drop(columns=["_sort_key"])
            return df
    except Exception as e:
        st.error(f"Erreur chargement chunks: {e}")
        return pd.DataFrame()


# ============================================================================
# SECTION EXPLORER FUNCTIONS
# ============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def get_section_documents() -> pd.DataFrame:
    """Get documents from rag_documents with section counts."""
    engine = get_db_engine()
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT d.doc_id, d.short_id, d.title, d.publisher, d.doc_type,
                       d.token_count, d.source_url,
                       COUNT(s.section_id) as section_count
                FROM rag_documents d
                LEFT JOIN rag_sections s ON s.doc_id = d.doc_id
                GROUP BY d.doc_id, d.short_id, d.title, d.publisher, d.doc_type,
                         d.token_count, d.source_url
                ORDER BY d.publisher, d.title
            """))
            return pd.DataFrame(result.fetchall(), columns=result.keys())
    except Exception as e:
        st.error(f"Erreur chargement documents: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def get_sections_for_document(doc_id: str) -> pd.DataFrame:
    """Get all sections for a document, ordered by section_index."""
    engine = get_db_engine()
    if not engine:
        return pd.DataFrame()
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT section_id, heading, heading_path, level, section_index,
                       token_count, char_count, is_indexable,
                       LEFT(section_markdown, 5000) as content
                FROM rag_sections
                WHERE doc_id = :doc_id
                ORDER BY section_index NULLS LAST, level, heading
            """), {"doc_id": doc_id})
            return pd.DataFrame(result.fetchall(), columns=result.keys())
    except Exception as e:
        st.error(f"Erreur chargement sections: {e}")
        return pd.DataFrame()


# ============================================================================
# MAIN UI
# ============================================================================

st.title("🗄️ DB Explorer")
st.markdown("Explorez les chunks et sections de la base de données RAG.")

engine = get_db_engine()
if not engine:
    st.error("Connexion a la base de données impossible. Vérifiez `APP_POSTGRES_DSN` ou les DSN historiques (`SCALINGO_POSTGRESQL_URL`, `PG_DSN`, `DATABASE_URL`, `SCW_POSTGRES_DSN`).")
    st.stop()

st.sidebar.success("Connecté à la base de données")

# ============================================================================
# TABS
# ============================================================================

tab_chunks, tab_sections = st.tabs(["🧩 Chunk Explorer", "📑 Section Explorer"])

# ============================================================================
# TAB 1: CHUNK EXPLORER
# ============================================================================

with tab_chunks:
    # Multi-selector for tables
    all_table_keys = list(TABLE_CONFIG.keys())
    all_table_labels = {k: TABLE_CONFIG[k]["display_name"] for k in all_table_keys}

    selected_tables = st.multiselect(
        "Tables à explorer",
        options=all_table_keys,
        default=DEFAULT_TABLES,
        format_func=lambda k: all_table_labels[k],
        key="chunk_table_selector",
    )

    if not selected_tables:
        st.info("Sélectionnez au moins une table.")
    elif selected_tables:
        stats = get_multi_table_stats(selected_tables)
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-value">{stats['unique_titles']:,}</div>
                <div class="stat-label">Documents uniques</div>
            </div>
            """, unsafe_allow_html=True)
        with col_s2:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-value">{stats['total_chunks']:,}</div>
                <div class="stat-label">Chunks total</div>
            </div>
            """, unsafe_allow_html=True)

        st.divider()

        documents_df = get_multi_table_documents(selected_tables)

        if documents_df.empty:
            st.warning("Aucun document trouvé dans les tables sélectionnées.")
        else:
            if "sel_chunk_doc" not in st.session_state:
                st.session_state.sel_chunk_doc = None
                st.session_state.sel_chunk_table = None

            col_docs, col_chunks = st.columns([1, 2])

            with col_docs:
                st.markdown("#### Documents")

                search = st.text_input("Rechercher un document", placeholder="Tapez pour filtrer...", key="chunk_search")
                if search:
                    search_normalized = normalize_text(search)
                    mask = documents_df["title"].apply(lambda x: search_normalized in normalize_text(str(x)))
                    filtered_df = documents_df[mask]
                    st.caption(f"{len(filtered_df)} document(s) trouvé(s)")
                else:
                    filtered_df = documents_df

                show_table_badge = len(selected_tables) > 1

                for idx, row in filtered_df.head(100).iterrows():
                    title = row["title"]
                    chunk_count = row["chunk_count"]
                    doc_table = row["table"]
                    badge = f"{row['table_display']} " if show_table_badge else ""

                    label_title = f"{title[:45]}{'...' if len(str(title)) > 45 else ''}"
                    button_label = f"{badge}{label_title} — {chunk_count} chunks"

                    is_selected = (
                        st.session_state.sel_chunk_doc == title
                        and st.session_state.sel_chunk_table == doc_table
                    )

                    if st.button(
                        button_label,
                        key=f"cdoc_{idx}_{doc_table}_{title[:15]}",
                        width="stretch",
                        type="primary" if is_selected else "secondary",
                    ):
                        st.session_state.sel_chunk_doc = title
                        st.session_state.sel_chunk_table = doc_table
                        st.rerun()

                if len(filtered_df) > 100:
                    st.caption("Seuls les 100 premiers documents sont affichés. Utilisez la recherche pour filtrer.")

            with col_chunks:
                st.markdown("#### Chunks du document")

                if st.session_state.sel_chunk_doc and st.session_state.sel_chunk_table:
                    chunk_table = st.session_state.sel_chunk_table
                    chunks_df = get_chunks_for_document(chunk_table, st.session_state.sel_chunk_doc)

                    if chunks_df.empty:
                        st.info("Aucun chunk trouvé pour ce document.")
                    else:
                        config = TABLE_CONFIG[chunk_table]
                        text_col = config["text_column"]
                        order_col = config["order_column"]
                        id_col = config["id_column"]

                        st.markdown(f"**{st.session_state.sel_chunk_doc}**")
                        st.caption(f"{len(chunks_df)} chunk(s) • Table: `{chunk_table}` • Ordonné par `{order_col}`")

                        for idx, row in chunks_df.iterrows():
                            chunk_id = row.get(id_col, idx)
                            order_value = row.get(order_col, idx)
                            chunk_text = row.get(text_col, "")
                            chunk_text_escaped = html.escape(str(chunk_text)) if chunk_text else ""

                            extra_cols = config.get("extra_columns", [])
                            meta_parts = []
                            for col in extra_cols:
                                if col in row and row[col]:
                                    meta_parts.append(f"{html.escape(col)}: {html.escape(str(row[col])[:50])}")
                            meta_str = " | ".join(meta_parts) if meta_parts else ""

                            st.markdown(f"""
                            <div class="chunk-card">
                                <div class="chunk-header">
                                    <span class="chunk-index">#{order_value}</span>
                                    <span class="chunk-meta">ID: {chunk_id}</span>
                                </div>
                                {f'<div class="chunk-meta" style="margin-bottom: 8px;">{meta_str}</div>' if meta_str else ''}
                                <div class="chunk-text">{chunk_text_escaped}</div>
                            </div>
                            """, unsafe_allow_html=True)
                else:
                    st.info("Sélectionnez un document dans la liste pour voir ses chunks.")

# ============================================================================
# TAB 2: SECTION EXPLORER
# ============================================================================

with tab_sections:
    docs_df = get_section_documents()

    if docs_df.empty:
        st.warning("Aucun document trouvé dans rag_documents.")

    if not docs_df.empty:
        total_docs = len(docs_df)
        total_sections = int(docs_df["section_count"].sum())
        publishers = docs_df["publisher"].nunique()

        col_ds1, col_ds2, col_ds3 = st.columns(3)
        with col_ds1:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-value">{total_docs}</div>
                <div class="stat-label">Documents</div>
            </div>
            """, unsafe_allow_html=True)
        with col_ds2:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-value">{total_sections:,}</div>
                <div class="stat-label">Sections</div>
            </div>
            """, unsafe_allow_html=True)
        with col_ds3:
            st.markdown(f"""
            <div class="stat-box">
                <div class="stat-value">{publishers}</div>
                <div class="stat-label">Publishers</div>
            </div>
            """, unsafe_allow_html=True)

        st.divider()

        all_publishers = sorted(docs_df["publisher"].dropna().unique().tolist())
        selected_publishers = st.multiselect(
            "Filtrer par publisher",
            options=all_publishers,
            default=all_publishers,
            key="section_publisher_filter",
        )

        if "sel_sec_doc_id" not in st.session_state:
            st.session_state.sel_sec_doc_id = None

        col_sdocs, col_sects = st.columns([1, 2])

        with col_sdocs:
            st.markdown("#### Documents")

            search_sec = st.text_input("Rechercher un document", placeholder="Tapez pour filtrer...", key="section_search")

            filtered_docs = docs_df[docs_df["publisher"].isin(selected_publishers)] if selected_publishers else docs_df
            if search_sec:
                search_norm = normalize_text(search_sec)
                filtered_docs = filtered_docs[filtered_docs["title"].apply(lambda x: search_norm in normalize_text(str(x)))]

            st.caption(f"{len(filtered_docs)} document(s)")

            for idx, row in filtered_docs.head(100).iterrows():
                pub_emoji = "📋" if row["publisher"] == "MATTE" else "🏛️"
                label = f"{pub_emoji} {row['title'][:40]}{'...' if len(str(row['title'])) > 40 else ''} — {row['section_count']} sections"

                is_selected = st.session_state.sel_sec_doc_id == str(row["doc_id"])

                if st.button(
                    label,
                    key=f"sdoc_{idx}_{str(row['doc_id'])[:8]}",
                    width="stretch",
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state.sel_sec_doc_id = str(row["doc_id"])
                    st.rerun()

            if len(filtered_docs) > 100:
                st.caption("Seuls les 100 premiers documents sont affichés.")

        with col_sects:
            st.markdown("#### Sections du document")

            if st.session_state.sel_sec_doc_id:
                doc_row = docs_df[docs_df["doc_id"].astype(str) == st.session_state.sel_sec_doc_id]
                if not doc_row.empty:
                    doc_info = doc_row.iloc[0]
                    st.markdown(f"**{doc_info['title']}**")
                    meta_parts = [f"Publisher: {doc_info['publisher']}"]
                    if doc_info.get("short_id"):
                        meta_parts.append(f"ID: {doc_info['short_id']}")
                    if doc_info.get("doc_type"):
                        meta_parts.append(f"Type: {doc_info['doc_type']}")
                    if doc_info.get("token_count"):
                        meta_parts.append(f"{doc_info['token_count']:,} tokens")
                    st.caption(" • ".join(meta_parts))

                sections_df = get_sections_for_document(st.session_state.sel_sec_doc_id)

                if sections_df.empty:
                    st.info("Aucune section trouvée pour ce document.")
                else:
                    st.caption(f"{len(sections_df)} section(s)")

                    for idx, row in sections_df.iterrows():
                        heading = row.get("heading", "Sans titre") or "Sans titre"
                        level = row.get("level", 1) or 1
                        tokens = row.get("token_count", 0) or 0
                        indexable = row.get("is_indexable", True)
                        content = row.get("content", "") or ""

                        indent = "—" * (level - 1) + " " if level > 1 else ""
                        heading_escaped = html.escape(str(heading))
                        content_escaped = html.escape(str(content))
                        idx_badge = f'<span class="badge badge-count">{tokens} tokens</span>'
                        indexable_badge = "" if indexable else ' <span class="badge" style="background:#fee2e2;color:#991b1b;">non indexable</span>'

                        st.markdown(f"""
                        <div class="chunk-card">
                            <div class="chunk-header">
                                <span class="chunk-index">{indent}H{level} {heading_escaped}</span>
                                <span class="chunk-meta">{idx_badge}{indexable_badge}</span>
                            </div>
                            <div class="chunk-text" style="max-height: 300px; overflow-y: auto;">{content_escaped}</div>
                        </div>
                        """, unsafe_allow_html=True)
            else:
                st.info("Sélectionnez un document dans la liste pour voir ses sections.")

# ============================================================================
# FOOTER
# ============================================================================

st.divider()
st.caption("🗄️ DB Explorer • Chunks & Sections RAG")
