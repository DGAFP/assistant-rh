"""
Goldset Explorer - Visualisation, tagging et gestion du goldset de questions
"""
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from assistant_rh_rag_pipeline.db_helpers import get_dsn as get_app_dsn
from dotenv import load_dotenv

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.db_utils import get_engine

load_dotenv()

require_admin()
show_admin_badge()

# Page config
st.set_page_config(
    page_title="Goldset Explorer",
    page_icon="🏆",
    layout="wide"
)

st.title("🏆 Goldset Explorer")
st.markdown("Visualisation et catégorisation du goldset de questions pour l'évaluation RAG")

# =============================================================================
# Constants
# =============================================================================

PREDEFINED_TAGS = [
    # Standard capability tags
    "factual",       # question factuelle directe
    "procedural",    # procédure / démarche
    "comparative",   # différence ou comparaison
    "multi-hop",     # croisement de plusieurs sources
    "reasoning",     # raisonnement logique ou calcul
    "out-of-scope",  # hors périmètre RH
    "red-teaming",   # piège, adversarial
    "ambiguous",     # intention floue
    # Technical / custom tags
    "acronym",       # acronymes (RIFSEEP, CDI, CMO...)
    "legal-ref",     # référence juridique (article, décret)
    "cross-source",  # réponse multi-tables
    "temporal",      # aspect temporel (dates, délais)
]

DIFFICULTY_LEVELS = ["easy", "medium", "hard"]


# =============================================================================
# Database Connection
# =============================================================================

def _get_dsn():
    """Get DSN string for psycopg connections."""
    try:
        return get_app_dsn()
    except RuntimeError:
        return None


def _get_psycopg_conn():
    """Get a psycopg connection (for writes). Cached in session_state."""
    import psycopg
    from psycopg.rows import dict_row

    conn = st.session_state.get("_goldset_db_conn")
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            st.session_state["_goldset_db_conn"] = None

    dsn = _get_dsn()
    if not dsn:
        return None
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        st.session_state["_goldset_db_conn"] = conn
        return conn
    except Exception as e:
        st.error(f"❌ Connexion DB impossible: {e}")
        return None


# =============================================================================
# Data Loading
# =============================================================================

@st.cache_data(ttl=30)
def load_goldset_from_db():
    """Load goldset from database, including tags and difficulty."""
    from sqlalchemy import text as sa_text
    engine = get_engine()
    if not engine:
        return None

    try:
        query = sa_text("""
            SELECT 
                gq.id,
                gq.question,
                gq.theme,
                gq.source,
                gq.goldset_name,
                gq.gold_answer,
                gq.gold_sources,
                gq.comment,
                gq.original_turn_id,
                gq.created_at,
                gq.difficulty,
                gq.tags,
                (SELECT COUNT(*) FROM goldset_runs gr WHERE gr.question_id = gq.id) as run_count
            FROM goldset_questions_v2 gq
            ORDER BY gq.id ASC
        """)
        df = pd.read_sql(query, engine)
        # Ensure tags column is list (SQLAlchemy may return it as list already or as string)
        if 'tags' in df.columns:
            df['tags'] = df['tags'].apply(lambda x: x if isinstance(x, list) else ([] if x is None else x))
        else:
            df['tags'] = [[] for _ in range(len(df))]
        if 'difficulty' not in df.columns:
            df['difficulty'] = None
        return df
    except Exception as e:
        # If tags/difficulty columns don't exist yet, try without them
        if "difficulty" in str(e) or "tags" in str(e):
            st.warning("⚠️ Colonnes tags/difficulty non trouvées. Exécutez la migration `add_goldset_tags.sql`.")
            try:
                query_fallback = sa_text("""
                    SELECT 
                        gq.id, gq.question, gq.theme, gq.source, gq.goldset_name,
                        gq.gold_answer, gq.gold_sources, gq.comment,
                        gq.original_turn_id, gq.created_at,
                        (SELECT COUNT(*) FROM goldset_runs gr WHERE gr.question_id = gq.id) as run_count
                    FROM goldset_questions_v2 gq
                    ORDER BY gq.id ASC
                """)
                df = pd.read_sql(query_fallback, engine)
                df['tags'] = [[] for _ in range(len(df))]
                df['difficulty'] = None
                return df
            except Exception as e2:
                st.warning(f"⚠️ Erreur chargement: {e2}")
                return None
        st.warning(f"⚠️ Erreur chargement: {e}")
        return None


@st.cache_data(ttl=60)
def load_goldset_from_csv():
    """Load goldset from CSV file (fallback)."""
    csv_paths = [
        Path("data/goldsets/goldset_conso_enriched.csv"),
        Path("data/goldsets/goldset_conso.csv"),
    ]
    for path in csv_paths:
        if path.exists():
            df = pd.read_csv(path)
            if 'query' in df.columns:
                df = df.rename(columns={'query': 'question'})
            if 'tags' not in df.columns:
                df['tags'] = [[] for _ in range(len(df))]
            if 'difficulty' not in df.columns:
                df['difficulty'] = None
            return df, str(path)
    return None, None


def get_all_tags_in_db(df):
    """Extract all unique tags from the dataframe."""
    all_tags = set()
    for tags_list in df['tags']:
        if isinstance(tags_list, list):
            all_tags.update(tags_list)
    # Merge with predefined tags
    return sorted(set(PREDEFINED_TAGS) | all_tags)


# =============================================================================
# DB Write operations
# =============================================================================

def update_question_tags(question_ids: list, tags: list, mode: str = "set"):
    """Update tags for questions. mode: 'set' (replace), 'add', 'remove'."""
    conn = _get_psycopg_conn()
    if not conn:
        st.error("Pas de connexion DB")
        return False

    try:
        with conn.cursor() as cur:
            if mode == "set":
                cur.execute(
                    "UPDATE goldset_questions_v2 SET tags = %s WHERE id = ANY(%s)",
                    (tags, question_ids)
                )
            elif mode == "add":
                # Add tags without duplicates
                cur.execute(
                    """UPDATE goldset_questions_v2 
                       SET tags = (
                           SELECT array_agg(DISTINCT t) 
                           FROM unnest(COALESCE(tags, ARRAY[]::text[]) || %s::text[]) AS t
                       )
                       WHERE id = ANY(%s)""",
                    (tags, question_ids)
                )
            elif mode == "remove":
                cur.execute(
                    """UPDATE goldset_questions_v2 
                       SET tags = (
                           SELECT COALESCE(array_agg(t), ARRAY[]::text[])
                           FROM unnest(tags) AS t 
                           WHERE t != ALL(%s::text[])
                       )
                       WHERE id = ANY(%s)""",
                    (tags, question_ids)
                )
        return True
    except Exception as e:
        st.error(f"Erreur mise à jour tags: {e}")
        return False


def update_question_difficulty(question_ids: list, difficulty: str):
    """Update difficulty for questions. Pass None to clear."""
    conn = _get_psycopg_conn()
    if not conn:
        st.error("Pas de connexion DB")
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE goldset_questions_v2 SET difficulty = %s WHERE id = ANY(%s)",
                (difficulty, question_ids)
            )
        return True
    except Exception as e:
        st.error(f"Erreur mise à jour difficulté: {e}")
        return False


def update_question_goldset_name(question_ids: list, new_goldset_name: str):
    """Change goldset_name for selected questions."""
    conn = _get_psycopg_conn()
    if not conn:
        st.error("Pas de connexion DB")
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE goldset_questions_v2 SET goldset_name = %s WHERE id = ANY(%s)",
                (new_goldset_name, question_ids)
            )
        return True
    except Exception as e:
        st.error(f"Erreur mise à jour goldset_name: {e}")
        return False


# =============================================================================
# Main App
# =============================================================================

# Try loading from DB first, fallback to CSV
df_goldset = load_goldset_from_db()
data_source = "PostgreSQL"

if df_goldset is None or len(df_goldset) == 0:
    df_goldset, csv_path = load_goldset_from_csv()
    data_source = f"CSV ({csv_path})" if csv_path else "none"

if df_goldset is None or len(df_goldset) == 0:
    st.error("❌ Aucune donnée goldset trouvée.")
    st.stop()

st.success(f"📊 **{len(df_goldset)} questions** chargées depuis {data_source}")


# =============================================================================
# Sidebar Filters
# =============================================================================

st.sidebar.header("🔍 Filtres")

# Theme filter
themes = ["Tous"] + sorted([t for t in df_goldset['theme'].dropna().unique().tolist() if t])
selected_theme = st.sidebar.selectbox("Thème", themes)

# Source filter
sources = ["Tous"] + sorted([s for s in df_goldset['source'].dropna().unique().tolist() if s])
selected_source = st.sidebar.selectbox("Source", sources)

# Goldset name filter
goldset_names = ["Tous"] + sorted([g for g in df_goldset['goldset_name'].dropna().unique().tolist() if g])
selected_goldset = st.sidebar.selectbox("Goldset", goldset_names)

# Gold answer filter
gold_answer_options = ["Tous", "Avec gold_answer", "Sans gold_answer"]
selected_gold_answer = st.sidebar.selectbox("Gold Answer", gold_answer_options)

# Difficulty filter
difficulty_options = ["Tous"] + DIFFICULTY_LEVELS
selected_difficulty = st.sidebar.selectbox("Difficulté", difficulty_options)

# Tags filter
all_tags = get_all_tags_in_db(df_goldset)
selected_tags = st.sidebar.multiselect("Tags (capacité)", all_tags)

# Tag filter mode
tag_filter_mode = "Tous"
if selected_tags:
    tag_filter_mode = st.sidebar.radio(
        "Mode filtre tags",
        ["Contient au moins un", "Contient tous"],
        horizontal=True
    )

# Untagged filter
show_untagged = st.sidebar.checkbox("Afficher uniquement les non-taguées")

# Search
search_query = st.sidebar.text_input("🔎 Rechercher dans les questions")


# =============================================================================
# Apply filters
# =============================================================================

df_filtered = df_goldset.copy()

if selected_theme != "Tous":
    df_filtered = df_filtered[df_filtered['theme'] == selected_theme]

if selected_source != "Tous":
    df_filtered = df_filtered[df_filtered['source'] == selected_source]

if selected_goldset != "Tous":
    df_filtered = df_filtered[df_filtered['goldset_name'] == selected_goldset]

if selected_gold_answer == "Avec gold_answer":
    df_filtered = df_filtered[df_filtered['gold_answer'].notna() & (df_filtered['gold_answer'] != '')]
elif selected_gold_answer == "Sans gold_answer":
    df_filtered = df_filtered[df_filtered['gold_answer'].isna() | (df_filtered['gold_answer'] == '')]

if selected_difficulty != "Tous":
    df_filtered = df_filtered[df_filtered['difficulty'] == selected_difficulty]

if selected_tags:
    if tag_filter_mode == "Contient tous":
        df_filtered = df_filtered[df_filtered['tags'].apply(
            lambda tags: isinstance(tags, list) and all(t in tags for t in selected_tags)
        )]
    else:
        df_filtered = df_filtered[df_filtered['tags'].apply(
            lambda tags: isinstance(tags, list) and any(t in tags for t in selected_tags)
        )]

if show_untagged:
    df_filtered = df_filtered[
        df_filtered['tags'].apply(lambda t: not isinstance(t, list) or len(t) == 0)
        & df_filtered['difficulty'].isna()
    ]

if search_query:
    df_filtered = df_filtered[df_filtered['question'].str.contains(search_query, case=False, na=False)]

st.sidebar.markdown(f"**{len(df_filtered)}** questions après filtrage")


# =============================================================================
# Stats Overview (KPIs)
# =============================================================================

col1, col2, col3, col4, col5, col6 = st.columns(6)

with col1:
    st.metric("Total", len(df_filtered))

with col2:
    with_gold = df_filtered['gold_answer'].notna() & (df_filtered['gold_answer'] != '')
    st.metric("Avec Gold Answer", int(with_gold.sum()))

with col3:
    n_tagged = df_filtered['tags'].apply(lambda t: isinstance(t, list) and len(t) > 0).sum()
    st.metric("Taguées", int(n_tagged))

with col4:
    n_with_diff = df_filtered['difficulty'].notna().sum()
    st.metric("Avec difficulté", int(n_with_diff))

with col5:
    themes_count = df_filtered['theme'].nunique()
    st.metric("Thèmes", themes_count)

with col6:
    n_untagged = len(df_filtered) - int(n_tagged)
    st.metric("Non taguées", n_untagged)


# =============================================================================
# Tabs
# =============================================================================

tab_browse, tab_categorize, tab_stats = st.tabs([
    "📋 Questions", "🏷️ Catégorisation", "📊 Stats par capacité"
])


# =============================================================================
# Tab 1: Browse Questions
# =============================================================================

with tab_browse:
    # Prepare tags display column
    df_display = df_filtered.copy()
    df_display['tags_str'] = df_display['tags'].apply(
        lambda t: ", ".join(t) if isinstance(t, list) and t else ""
    )

    display_cols = ['id', 'question', 'theme', 'tags_str',
                    'goldset_name', 'gold_sources', 'source']
    if 'run_count' in df_display.columns:
        display_cols.append('run_count')

    display_cols = [c for c in display_cols if c in df_display.columns]

    column_config = {
        "id": st.column_config.NumberColumn("ID", width="small"),
        "question": st.column_config.TextColumn("Question", width="large"),
        "theme": st.column_config.TextColumn("Thème", width="small"),
        "tags_str": st.column_config.TextColumn("Tags", width="medium"),
        "goldset_name": st.column_config.TextColumn("Goldset", width="small"),
        "gold_sources": st.column_config.TextColumn("Gold Sources", width="small"),
        "source": st.column_config.TextColumn("Source", width="small"),
        "run_count": st.column_config.NumberColumn("Runs", width="small"),
    }

    st.dataframe(
        df_display[display_cols],
        width="stretch",
        hide_index=True,
        height=600,
        column_config=column_config
    )

    # Export buttons
    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        csv_export = df_filtered.to_csv(index=False)
        st.download_button(
            label="📥 Télécharger CSV filtré",
            data=csv_export,
            file_name=f"goldset_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
    with col_exp2:
        eval_cols = ['id', 'question', 'theme', 'difficulty', 'gold_answer', 'gold_sources']
        eval_df = df_filtered[[c for c in eval_cols if c in df_filtered.columns]]
        eval_csv = eval_df.to_csv(index=False)
        st.download_button(
            label="📥 Export pour évaluation",
            data=eval_csv,
            file_name=f"goldset_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )


# =============================================================================
# Tab 2: Categorization
# =============================================================================

with tab_categorize:
    if data_source != "PostgreSQL":
        st.warning("La catégorisation nécessite une connexion à la base de données.")
        st.stop()

    st.markdown("### Sélectionner et catégoriser les questions")
    st.caption(f"{len(df_filtered)} questions dans le filtre actuel")

    # --- Question selection ---
    # Build a compact dataframe for selection
    df_select = df_filtered[['id', 'question', 'theme', 'difficulty', 'tags', 'goldset_name']].copy()
    df_select['tags_str'] = df_select['tags'].apply(
        lambda t: ", ".join(t) if isinstance(t, list) and t else ""
    )
    df_select['question_short'] = df_select['question'].str[:120]

    # Selection mode
    selection_mode = st.radio(
        "Mode de sélection",
        ["Sélection manuelle", "Toutes les questions filtrées"],
        horizontal=True
    )

    selected_ids = []

    if selection_mode == "Sélection manuelle":
        # Use data_editor with checkboxes
        df_check = df_select[['id', 'question_short', 'theme', 'difficulty', 'tags_str', 'goldset_name']].copy()
        df_check.insert(0, 'select', False)

        edited_df = st.data_editor(
            df_check,
            width="stretch",
            hide_index=True,
            height=400,
            column_config={
                "select": st.column_config.CheckboxColumn("✓", width="small", default=False),
                "id": st.column_config.NumberColumn("ID", width="small"),
                "question_short": st.column_config.TextColumn("Question", width="large"),
                "theme": st.column_config.TextColumn("Thème", width="small"),
                "difficulty": st.column_config.TextColumn("Diff.", width="small"),
                "tags_str": st.column_config.TextColumn("Tags", width="medium"),
                "goldset_name": st.column_config.TextColumn("Goldset", width="small"),
            },
            disabled=['id', 'question_short', 'theme', 'difficulty', 'tags_str', 'goldset_name'],
            key="question_selector"
        )
        selected_ids = edited_df[edited_df['select']]['id'].tolist()
    else:
        selected_ids = df_filtered['id'].tolist()

    n_selected = len(selected_ids)
    st.info(f"**{n_selected}** question(s) sélectionnée(s)")

    if n_selected == 0:
        st.warning("Sélectionnez au moins une question pour appliquer des actions.")
    else:
        st.markdown("---")

        # --- Actions ---
        col_action1, col_action2 = st.columns(2)

        with col_action1:
            st.markdown("#### 🎯 Difficulté")
            new_difficulty = st.selectbox(
                "Assigner une difficulté",
                ["(ne pas changer)", "easy", "medium", "hard", "(effacer)"],
                key="bulk_difficulty"
            )
            if st.button(f"Appliquer difficulté à {n_selected} question(s)", key="btn_difficulty"):
                if new_difficulty == "(ne pas changer)":
                    st.info("Aucun changement.")
                else:
                    diff_value = None if new_difficulty == "(effacer)" else new_difficulty
                    if update_question_difficulty(selected_ids, diff_value):
                        st.success(f"Difficulté mise à jour pour {n_selected} questions")
                        load_goldset_from_db.clear()
                        st.rerun()

        with col_action2:
            st.markdown("#### 🏷️ Tags")
            tag_action = st.radio(
                "Action sur les tags",
                ["Ajouter", "Retirer", "Remplacer"],
                horizontal=True,
                key="tag_action_mode"
            )

            # Merge predefined + existing + allow custom
            all_available_tags = get_all_tags_in_db(df_goldset)
            chosen_tags = st.multiselect(
                "Tags à appliquer",
                all_available_tags,
                key="bulk_tags"
            )

            # Custom tag input
            custom_tag = st.text_input("Ou créer un tag custom", key="custom_tag",
                                       placeholder="ex: mon-tag-custom")
            if custom_tag:
                custom_tag = custom_tag.strip().lower().replace(" ", "-")
                if custom_tag and custom_tag not in chosen_tags:
                    chosen_tags.append(custom_tag)

            if chosen_tags:
                mode_map = {"Ajouter": "add", "Retirer": "remove", "Remplacer": "set"}
                if st.button(
                    f"{tag_action} {len(chosen_tags)} tag(s) sur {n_selected} question(s)",
                    key="btn_tags"
                ):
                    if update_question_tags(selected_ids, chosen_tags, mode=mode_map[tag_action]):
                        st.success(f"Tags mis à jour pour {n_selected} questions")
                        load_goldset_from_db.clear()
                        st.rerun()

        st.markdown("---")

        # --- Goldset name change ---
        st.markdown("#### 📁 Changer le goldset_name")
        col_gs1, col_gs2 = st.columns([2, 1])
        with col_gs1:
            new_gs_name = st.text_input(
                "Nouveau goldset_name",
                key="new_goldset_name",
                placeholder="ex: eval_acronyms_v1"
            )
        with col_gs2:
            if new_gs_name and st.button(
                f"Assigner à {n_selected} question(s)", key="btn_goldset_name"
            ):
                if update_question_goldset_name(selected_ids, new_gs_name.strip()):
                    st.success(f"goldset_name mis à jour pour {n_selected} questions")
                    load_goldset_from_db.clear()
                    st.rerun()

        st.markdown("---")

        # --- Single question editor ---
        st.markdown("#### ✏️ Édition unitaire")
        q_options = {
            f"Q{row['id']}: {row['question'][:100]}": row['id']
            for _, row in df_filtered[df_filtered['id'].isin(selected_ids)].head(50).iterrows()
        }
        if q_options:
            chosen_q_label = st.selectbox("Question à éditer", list(q_options.keys()), key="single_edit_q")
            chosen_q_id = q_options[chosen_q_label]
            q_row = df_goldset[df_goldset['id'] == chosen_q_id].iloc[0]

            col_e1, col_e2 = st.columns(2)
            with col_e1:
                current_diff = q_row['difficulty'] if pd.notna(q_row['difficulty']) else "(aucune)"
                edit_diff = st.selectbox(
                    f"Difficulté (actuelle: {current_diff})",
                    ["(ne pas changer)", "easy", "medium", "hard", "(effacer)"],
                    key=f"edit_diff_{chosen_q_id}"
                )
            with col_e2:
                current_tags = q_row['tags'] if isinstance(q_row['tags'], list) else []
                edit_tags = st.multiselect(
                    f"Tags (actuels: {', '.join(current_tags) or 'aucun'})",
                    all_available_tags,
                    default=current_tags,
                    key=f"edit_tags_{chosen_q_id}"
                )

            if st.button("💾 Sauvegarder", key=f"save_single_{chosen_q_id}"):
                changed = False
                if edit_diff != "(ne pas changer)":
                    diff_val = None if edit_diff == "(effacer)" else edit_diff
                    update_question_difficulty([chosen_q_id], diff_val)
                    changed = True
                if set(edit_tags) != set(current_tags):
                    update_question_tags([chosen_q_id], edit_tags, mode="set")
                    changed = True
                if changed:
                    st.success(f"Question Q{chosen_q_id} mise à jour")
                    load_goldset_from_db.clear()
                    st.rerun()
                else:
                    st.info("Aucun changement détecté")


# =============================================================================
# Tab 3: Stats by Capability
# =============================================================================

with tab_stats:
    st.markdown("### Distribution des tags et de la difficulté")

    # Use full goldset (not filtered) for stats unless user wants filtered
    use_filtered_stats = st.checkbox("Appliquer les filtres aux stats", value=False)
    df_stats = df_filtered if use_filtered_stats else df_goldset

    col_s1, col_s2 = st.columns(2)

    # --- Tag distribution ---
    with col_s1:
        st.markdown("#### Tags (capacités)")
        tag_counter = Counter()
        for tags_list in df_stats['tags']:
            if isinstance(tags_list, list):
                tag_counter.update(tags_list)

        if tag_counter:
            df_tags = pd.DataFrame(
                sorted(tag_counter.items(), key=lambda x: -x[1]),
                columns=["Tag", "Questions"]
            )
            st.bar_chart(df_tags.set_index("Tag"))
            st.dataframe(df_tags, width="stretch", hide_index=True)
        else:
            st.info("Aucune question taguée pour l'instant.")

    # --- Difficulty distribution ---
    with col_s2:
        st.markdown("#### Difficulté")
        diff_counts = df_stats['difficulty'].value_counts().reindex(DIFFICULTY_LEVELS).fillna(0).astype(int)
        n_no_diff = df_stats['difficulty'].isna().sum()
        diff_counts["(aucune)"] = n_no_diff

        df_diff = diff_counts.reset_index()
        df_diff.columns = ["Difficulté", "Questions"]
        st.bar_chart(df_diff.set_index("Difficulté"))
        st.dataframe(df_diff, width="stretch", hide_index=True)

    st.markdown("---")

    # --- Coverage matrix: tags x goldset_name ---
    st.markdown("#### Matrice de couverture : Tags × Goldset")

    goldset_names_list = sorted([g for g in df_stats['goldset_name'].dropna().unique() if g])
    if goldset_names_list and tag_counter:
        matrix_data = {}
        for gs_name in goldset_names_list:
            df_gs = df_stats[df_stats['goldset_name'] == gs_name]
            gs_tag_count = Counter()
            for tags_list in df_gs['tags']:
                if isinstance(tags_list, list):
                    gs_tag_count.update(tags_list)
            matrix_data[gs_name] = gs_tag_count

        all_matrix_tags = sorted(tag_counter.keys())
        matrix_rows = []
        for tag in all_matrix_tags:
            row = {"Tag": tag}
            for gs_name in goldset_names_list:
                row[gs_name] = matrix_data[gs_name].get(tag, 0)
            row["Total"] = tag_counter[tag]
            matrix_rows.append(row)

        df_matrix = pd.DataFrame(matrix_rows)
        st.dataframe(df_matrix, width="stretch", hide_index=True, height=400)
    else:
        st.info("Pas assez de données pour la matrice de couverture.")

    st.markdown("---")

    # --- Difficulty x Goldset ---
    st.markdown("#### Difficulté × Goldset")
    if goldset_names_list:
        diff_matrix_rows = []
        for diff in DIFFICULTY_LEVELS + ["(aucune)"]:
            row = {"Difficulté": diff}
            for gs_name in goldset_names_list:
                df_gs = df_stats[df_stats['goldset_name'] == gs_name]
                if diff == "(aucune)":
                    row[gs_name] = int(df_gs['difficulty'].isna().sum())
                else:
                    row[gs_name] = int((df_gs['difficulty'] == diff).sum())
            diff_matrix_rows.append(row)

        df_diff_matrix = pd.DataFrame(diff_matrix_rows)
        st.dataframe(df_diff_matrix, width="stretch", hide_index=True)

    st.markdown("---")

    # --- Untagged questions ---
    st.markdown("#### Questions non catégorisées")
    df_untagged = df_stats[
        df_stats['tags'].apply(lambda t: not isinstance(t, list) or len(t) == 0)
        & df_stats['difficulty'].isna()
    ]
    st.metric("Questions sans tag ni difficulté", len(df_untagged))

    if len(df_untagged) > 0:
        with st.expander(f"Voir les {min(len(df_untagged), 50)} premières non catégorisées"):
            display_untag = df_untagged[['id', 'question', 'theme', 'goldset_name']].head(50)
            st.dataframe(display_untag, width="stretch", hide_index=True)
