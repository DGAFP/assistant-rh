"""
📊 Dashboard Feedback — Suivi des retours utilisateurs.

Filtrage par groupe de testeurs, période, thème.
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.db_utils import get_engine
from src.ui.feedback_dashboard import (
    BETA_START,
    MINISTRY_NOT_SET_LABEL,
    PERIOD_CUSTOM,
    PERIOD_MODE_LABELS,
    PERIOD_MODE_OPTIONS,
    ministry_display_label,
    period_caption,
    resolve_period,
    visible_available_groups,
)
from src.ui.user_groups_store import group_chart_maps, list_groups

try:
    st.set_page_config(page_title="Feedback Dashboard", page_icon="📊", layout="wide")
except Exception:
    pass

require_admin()
show_admin_badge()


def dataframe_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str) -> bytes:
    """Serialize a dataframe to an in-memory XLSX file."""
    buffer = BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("openpyxl"):
            raise RuntimeError("Le package openpyxl est requis pour l'export Excel.") from exc
        raise
    return buffer.getvalue()


# ------------------------------
# Data loading
# ------------------------------
@st.cache_data(ttl=60)
def load_feedbacks_with_groups() -> pd.DataFrame:
    """
    Load feedbacks enriched with user_group from chat_runs.
    Uses JOIN to connect feedbacks with their corresponding runs.
    Includes AI analysis results (error_category, ai_reason).
    """
    engine = get_engine()
    if not engine:
        st.error("❌ Connexion PostgreSQL impossible")
        return pd.DataFrame()

    try:
        query = """
            SELECT 
                f.id,
                f.ts,
                f.turn_id,
                f.turn_idx,
                f.helpful,
                f.reasons,
                f.reasons_positive,
                f.reasons_negative,
                f.comment,
                f.stars,
                f.session_id,
                f.question,
                f.answer,
                f.error_category,
                f.ai_reason,
                f.ai_analyzed_at,
                f.beta_scope,
                COALESCE(r.v3_detected_theme, f.theme) as theme,
                r.user_group,
                r.selected_ministry,
                r.dist_after_rerank,
                r.rag_version,
                r.chunk_selection_mode
            FROM chat_feedbacks f
            LEFT JOIN chat_runs r ON f.turn_id = r.turn_id
            ORDER BY f.ts DESC
        """
        df = pd.read_sql(query, engine)
        return df
    except Exception as e:
        st.error(f"Erreur chargement feedbacks: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_questions_stats() -> pd.DataFrame:
    """Load all questions (chat_runs) for usage statistics."""
    engine = get_engine()
    if not engine:
        return pd.DataFrame()

    try:
        query = """
            SELECT 
                ts,
                turn_id,
                session_id,
                user_group,
                selected_ministry,
                question,
                answer,
                rag_version,
                chunk_selection_mode,
                dist_after_rerank,
                total_time_ms
            FROM chat_runs
            ORDER BY ts DESC
        """
        df = pd.read_sql(query, engine)
        return df
    except Exception as e:
        st.error(f"Erreur chargement questions: {e}")
        return pd.DataFrame()


# ------------------------------
# Data processing
# ------------------------------
def process_feedbacks(df: pd.DataFrame) -> pd.DataFrame:
    """Process and enrich feedback data."""
    if df.empty:
        return df

    # Convert timestamps to Paris timezone
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Paris")
        df["date"] = df["ts"].dt.date
        df["hour"] = df["ts"].dt.hour

    # Parse reasons into lists
    def parse_reasons(val):
        if pd.isna(val) or val == "":
            return []
        return [x.strip() for x in str(val).split(";") if x.strip()]

    if "reasons_positive" in df.columns:
        df["reasons_positive_list"] = df["reasons_positive"].apply(parse_reasons)
    else:
        df["reasons_positive_list"] = [[] for _ in range(len(df))]

    if "reasons_negative" in df.columns:
        df["reasons_negative_list"] = df["reasons_negative"].apply(parse_reasons)
    else:
        df["reasons_negative_list"] = [[] for _ in range(len(df))]

    # Create display label for ratings
    def get_label(row):
        if pd.notna(row.get("stars")):
            stars = int(row["stars"])
            return "⭐" * (stars + 1)  # 0=1★, 4=5★
        elif pd.notna(row.get("helpful")):
            return "👍" if row["helpful"] else "👎"
        return "—"

    df["label"] = df.apply(get_label, axis=1)

    # Satisfaction category
    def get_satisfaction(row):
        if pd.notna(row.get("stars")):
            stars = int(row["stars"])
            return "Satisfait" if stars >= 3 else "Insatisfait"
        elif pd.notna(row.get("helpful")):
            return "Satisfait" if row["helpful"] else "Insatisfait"
        return "Unknown"

    df["satisfaction"] = df.apply(get_satisfaction, axis=1)

    # Fill missing user_group
    if "user_group" in df.columns:
        df["user_group"] = df["user_group"].fillna("unknown")
    else:
        df["user_group"] = "unknown"

    return df


def process_questions(df: pd.DataFrame) -> pd.DataFrame:
    """Process questions data for stats."""
    if df.empty:
        return df

    # Convert timestamps to Paris timezone
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Paris")
        df["date"] = df["ts"].dt.date

    if "user_group" in df.columns:
        df["user_group"] = df["user_group"].fillna("unknown")
    else:
        df["user_group"] = "unknown"

    return df


# ------------------------------
# Group display helpers
# ------------------------------
# Couleurs/labels par groupe — DB-authoritative (fallback seed via user_groups_store),
# pour que les groupes créés par un admin s'affichent avec leurs propres couleurs.
GROUP_COLORS, GROUP_LABELS = group_chart_maps()

# Labels pour les catégories d'erreur (identifiées par LLM)
ERROR_CATEGORY_LABELS = {
    "retrieval_issue": "🔍 Retrieval: mauvais chunks",
    "candidate_cut": "📉 Agrégation: coupé avant sélection",
    "selector_misunderstanding": "🟡 Selector: mauvaise compréhension",
    "selector_wrong_priority": "🟡 Selector: mauvaise priorité",
    "generator_hallucination": "🔴 Generator: hallucination",
    "generator_incomplete": "🟠 Generator: réponse incomplète",
    "generator_wrong_interpretation": "🟠 Generator: mauvaise interprétation",
    "missing_document": "📄 Document manquant",
    "chunk_quality": "📊 Qualité des chunks",
    "other": "❓ Autre",
}

ERROR_CATEGORY_COLORS = {
    "retrieval_issue": "#00CC96",  # Vert (retrieval)
    "candidate_cut": "#19D3F3",  # Cyan (agrégation / coupe des candidats)
    "selector_misunderstanding": "#FECB52",  # Jaune
    "selector_wrong_priority": "#FFA15A",  # Orange
    "generator_hallucination": "#EF553B",  # Rouge
    "generator_incomplete": "#FFA15A",  # Orange
    "generator_wrong_interpretation": "#FFA15A",  # Orange
    "missing_document": "#636EFA",  # Bleu
    "chunk_quality": "#AB63FA",  # Violet
    "other": "#888888",  # Gris
}

# Labels pour les thèmes de questions (alignés avec les thèmes métier)
THEME_LABELS = {
    "recrutement": "📋 Recrutement",
    "typologie_contrats": "📑 Typologie des contrats",
    "remuneration": "💰 Rémunération & Fiche de paye",
    "renouvellement_mobilite": "🔄 Renouvellement / Mobilité",
    "fin_contrat_licenciement": "🚪 Fin de contrat / Licenciement",
    "temps_de_travail": "⏰ Temps de travail",
    "conges": "🏖️ Congés",
    "formation": "🎓 Formation",
    "action_sociale": "🤝 Action sociale",
    "psc": "🏥 PSC",
    "sante_securite": "🩺 Santé & Sécurité au travail",
    "retraite": "🏛️ Retraite",
    "deontologie": "⚖️ Déontologie",
    "autre": "❓ Autre",
}

THEME_COLORS = {
    "recrutement": "#636EFA",  # Bleu
    "typologie_contrats": "#AB63FA",  # Violet
    "remuneration": "#00CC96",  # Vert
    "renouvellement_mobilite": "#19D3F3",  # Cyan
    "fin_contrat_licenciement": "#EF553B",  # Rouge
    "temps_de_travail": "#FF6692",  # Rose
    "conges": "#FFA15A",  # Orange
    "formation": "#FECB52",  # Jaune
    "action_sociale": "#B6E880",  # Vert clair
    "psc": "#FF97FF",  # Rose clair
    "sante_securite": "#00D4AA",  # Turquoise
    "retraite": "#D4A574",  # Marron doré
    "deontologie": "#7B68EE",  # Slate blue
    "autre": "#888888",  # Gris
}


def get_group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group)


def get_error_category_label(category: str) -> str:
    if not category or category == "—":
        return "Non analysé"
    return ERROR_CATEGORY_LABELS.get(category, category)


def get_theme_label(theme: str) -> str:
    return THEME_LABELS.get(theme, theme or "—")


# ------------------------------
# Main App
# ------------------------------
st.title("📊 Dashboard Feedback")
st.caption("Suivi des retours utilisateurs")

# Load data
with st.spinner("Chargement des données..."):
    df_feedbacks_raw = load_feedbacks_with_groups()
    df_questions_raw = load_questions_stats()
    df_feedbacks = process_feedbacks(df_feedbacks_raw)
    df_questions = process_questions(df_questions_raw)

if df_questions.empty and df_feedbacks.empty:
    st.error("❌ Aucune donnée trouvée dans PostgreSQL")
    st.info("Assurez-vous que les tables `chat_runs` et `chat_feedbacks` contiennent des données.")
    st.stop()

# ------------------------------
# Sidebar - Filters
# ------------------------------
with st.sidebar:
    st.subheader("🎯 Filtre par groupe")

    # Only include groups that both occur in the data and remain visible in the
    # User Groups admin. Hidden groups stay persisted but leave this selector.
    available_groups = visible_available_groups(df_questions["user_group"].unique(), list_groups())

    # Beta test groups first
    beta_groups = ["betatest-jan26", "mattebeta-jan26", "specloiret"]
    group_options = [g for g in beta_groups if g in available_groups]
    group_options += [g for g in available_groups if g not in beta_groups]

    # Default: only betatest-jan26 selected
    default_group = ["betatest-jan26"] if "betatest-jan26" in group_options else group_options[:1]

    selected_groups = st.multiselect(
        "Groupes de testeurs", options=group_options, default=default_group, format_func=get_group_label, help="Sélectionnez les groupes à analyser"
    )

    if not selected_groups:
        selected_groups = group_options  # All if none selected

    st.divider()

    st.subheader("📅 Période")

    period_mode = st.radio(
        "Mode de période",
        options=list(PERIOD_MODE_OPTIONS),
        format_func=PERIOD_MODE_LABELS.get,
        key="fb_period_mode",
        help="« Tout » suit les nouvelles données ; « Mois dernier » et « Beta-test » sont des périodes prédéfinies ; la période personnalisée conserve vos bornes.",
    )

    data_min = data_max = None
    if "date" in df_questions.columns and df_questions["date"].notna().any():
        data_min = df_questions["date"].min()
        data_max = df_questions["date"].max()

    custom_range = None
    if period_mode == PERIOD_CUSTOM:
        if "fb_custom_range" not in st.session_state:
            st.session_state["fb_custom_range"] = (data_min or BETA_START, data_max or date.today())
        custom_range = st.date_input("Période d'analyse", key="fb_custom_range")

    date_range = resolve_period(period_mode, custom_range, st.session_state.get("fb_custom_range_applied"))
    if period_mode == PERIOD_CUSTOM:
        if date_range is not None:
            st.session_state["fb_custom_range_applied"] = date_range
        if custom_range is not None and len(custom_range) != 2:
            st.warning("Sélection de période incomplète : choisissez une date de fin.")
    applied_period_caption = period_caption(period_mode, date_range, data_min, data_max)
    st.caption(f"Période appliquée : {applied_period_caption}")

    st.divider()

    st.subheader("🔍 Qualité RAG")
    st.caption("Labels ajoutés manuellement à la fin du beta-test, après validation du chef de projet.")
    exclude_missing_docs = st.checkbox(
        "Exclure 'Document manquant'", value=False, help="Exclut les feedbacks où le problème est un document manquant dans la base"
    )
    exclude_hors_perimetre = st.checkbox("Exclure 'Hors périmètre'", value=False, help="Exclut les feedbacks dont le périmètre beta-test est 'Non'")

    st.divider()

    # Theme filter
    st.subheader("🏷️ Filtre par thème")
    theme_options = [None]  # None = "Tous"
    if "theme" in df_feedbacks.columns and df_feedbacks["theme"].notna().any():
        theme_options += sorted(df_feedbacks["theme"].dropna().unique().tolist())
    selected_theme = st.selectbox(
        "Thème",
        options=theme_options,
        format_func=lambda x: "Tous" if x is None else get_theme_label(x),
        help="Afficher uniquement les feedbacks du thème sélectionné",
    )

    st.divider()

    # Refresh
    if st.button("🔄 Rafraîchir", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    # Data source info
    st.caption("📁 Source: PostgreSQL")
    st.caption(f"📊 {len(df_questions_raw)} questions, {len(df_feedbacks_raw)} feedbacks")

# Apply filters to data
df_q = df_questions[df_questions["user_group"].isin(selected_groups)].copy()
df_f = df_feedbacks[df_feedbacks["user_group"].isin(selected_groups)].copy()

if date_range and len(date_range) == 2:
    df_q = df_q[(df_q["date"] >= date_range[0]) & (df_q["date"] <= date_range[1])]
    df_f = df_f[(df_f["date"] >= date_range[0]) & (df_f["date"] <= date_range[1])]

# Snapshot for usage analytics (before theme filter - represents global usage)
df_q_usage = df_q.copy()
df_f_usage = df_f.copy()

# Theme filter
if selected_theme and "theme" in df_f.columns:
    df_f = df_f[df_f["theme"] == selected_theme].copy()
    if "turn_id" in df_q.columns and "turn_id" in df_f.columns:
        valid_turn_ids = df_f["turn_id"].unique()
        df_q = df_q[df_q["turn_id"].isin(valid_turn_ids)].copy()
    st.info(f"🏷️ **Thème sélectionné** : {get_theme_label(selected_theme)}. Stats affichées pour ce thème uniquement.")

# Quality RAG exclusions (missing_document, hors périmètre)
mask_missing = (df_f["error_category"] == "missing_document") if "error_category" in df_f.columns else pd.Series(False, index=df_f.index)
mask_hors = (
    (df_f["beta_scope"].fillna("").astype(str).str.strip().str.lower() == "non")
    if "beta_scope" in df_f.columns
    else pd.Series(False, index=df_f.index)
)

n_missing = int(mask_missing.sum())
n_hors = int(mask_hors.sum())

exclude_mask = pd.Series(False, index=df_f.index)
if exclude_missing_docs:
    exclude_mask = exclude_mask | mask_missing
if exclude_hors_perimetre:
    exclude_mask = exclude_mask | mask_hors

total_excluded = int(exclude_mask.sum())
if total_excluded > 0:
    parts = []
    if exclude_missing_docs and n_missing > 0:
        parts.append(f"{n_missing} 'Document manquant'")
    if exclude_hors_perimetre and n_hors > 0:
        parts.append(f"{n_hors} 'Hors périmètre'")
    detail = ", ".join(parts)
    total_str = f" — Total {total_excluded} exclus" if len(parts) > 1 else ""
    st.info(f"🔍 **Mode Qualité RAG** : {detail}{total_str}. Stats affichées sans ces feedbacks.")
    df_f = df_f[~exclude_mask].copy()

# ------------------------------
# KPIs - Top Metrics
# ------------------------------
st.subheader("📈 Métriques clés")

col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric("💬 Questions totales", len(df_q), help="Nombre de questions posées par les groupes sélectionnés")

with col2:
    st.metric("📝 Évaluations", len(df_f), help="Nombre de feedbacks reçus")

with col3:
    if len(df_f) > 0 and "stars" in df_f.columns and df_f["stars"].notna().any():
        avg_stars = df_f["stars"].mean() + 1  # +1 car 0=1★
        st.metric("⭐ Note moyenne", f"{avg_stars:.1f}/5", help="Note moyenne sur les évaluations avec étoiles")
    else:
        st.metric("⭐ Note moyenne", "—")

with col4:
    if "session_id" in df_q.columns and df_q["session_id"].notna().any():
        unique_testers = df_q["session_id"].nunique()
        st.metric("👥 Testeurs uniques", unique_testers, help="Nombre d'utilisateurs uniques (basé sur session_id persistant)")
    else:
        st.metric("👥 Testeurs uniques", "—")

with col5:
    if len(df_q) > 0 and len(df_f) > 0:
        eval_rate = len(df_f) / len(df_q) * 100
        st.metric("📊 Taux d'évaluation", f"{eval_rate:.1f}%", help="Pourcentage de questions ayant reçu un feedback")
    else:
        st.metric("📊 Taux d'évaluation", "—")

# with col6:
#     # Count analyzed feedbacks (those with error_category)
#     analyzed_count = 0
#     if "error_category" in df_f.columns:
#         analyzed_count = df_f["error_category"].notna().sum()
#     st.metric(
#         "🤖 Analysés par IA",
#         analyzed_count,
#         help="Feedbacks négatifs analysés automatiquement par LLM"
#     )

st.divider()

# ------------------------------
# Charts Row 1: Usage & Distribution
# ------------------------------
chart_col1, chart_col2 = st.columns([1.2, 1])

with chart_col1:
    st.subheader("📊 Questions par jour")
    if "date" in df_q.columns and df_q["date"].notna().any():
        daily_questions = df_q.groupby("date").size().reset_index(name="count")
        daily_questions = daily_questions.sort_values("date")

        fig = px.bar(
            daily_questions,
            x="date",
            y="count",
            labels={"date": "Date", "count": "Questions"},
        )
        fig.update_traces(marker_color="#0091FF")
        fig.update_layout(height=300, xaxis_title="", yaxis_title="Nombre de questions", showlegend=False)
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Pas de données temporelles disponibles")

with chart_col2:
    st.subheader("⭐ Distribution des notes")
    if "stars" in df_f.columns and df_f["stars"].notna().any():
        stars_display = df_f["stars"].dropna() + 1
        star_counts = stars_display.value_counts().sort_index()

        colors = {1: "#ff4444", 2: "#ffaa44", 3: "#ffdd44", 4: "#aadd44", 5: "#44dd44"}
        bar_colors = [colors.get(int(s), "#0091FF") for s in star_counts.index]

        fig = px.bar(
            x=[f"{int(s)}★" for s in star_counts.index],
            y=star_counts.values,
            labels={"x": "Note", "y": "Nombre"},
        )
        fig.update_traces(marker_color=bar_colors)
        fig.update_layout(height=300, showlegend=False)
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Aucune évaluation avec étoiles")

# ------------------------------
# Charts Row 2: Evolution & Satisfaction
# ------------------------------
chart_col3, chart_col4 = st.columns([1.2, 1])

with chart_col3:
    st.subheader("📈 Évolution satisfaction")
    if "date" in df_f.columns and df_f["date"].notna().any() and "satisfaction" in df_f.columns:
        daily_satisfaction = df_f.groupby(["date", "satisfaction"]).size().reset_index(name="count")

        if not daily_satisfaction.empty:
            fig = px.line(
                daily_satisfaction,
                x="date",
                y="count",
                color="satisfaction",
                markers=True,
                color_discrete_map={"Satisfait": "#44dd44", "Insatisfait": "#ff4444", "Unknown": "#888888"},
            )
            fig.update_layout(height=300, legend=dict(orientation="h", y=1.15, x=0.5, xanchor="center"), xaxis_title="", yaxis_title="Nombre")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Pas assez de données pour l'évolution")
    else:
        st.info("Pas de données temporelles")

with chart_col4:
    st.subheader("👥 Répartition par groupe")
    if "user_group" in df_q.columns:
        group_counts = df_q["user_group"].value_counts()

        # Créer le color_discrete_map avec les labels comme clés
        pie_colors = {get_group_label(g): GROUP_COLORS.get(g, "#888888") for g in group_counts.index}

        fig = px.pie(values=group_counts.values, names=[get_group_label(g) for g in group_counts.index], hole=0.4, color_discrete_map=pie_colors)
        fig.update_layout(height=300, showlegend=True)
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Pas de données de groupe")

# Theme distribution chart
if "theme" in df_f.columns and df_f["theme"].notna().any():
    st.subheader("🏷️ Répartition par thème")
    theme_counts = df_f["theme"].dropna().value_counts()

    # Create labels and colors
    theme_labels = [get_theme_label(t) for t in theme_counts.index]
    theme_pie_colors = {get_theme_label(t): THEME_COLORS.get(t, "#888888") for t in theme_counts.index}

    fig = px.pie(values=theme_counts.values, names=theme_labels, hole=0.4, color_discrete_map=theme_pie_colors)
    fig.update_layout(height=350, showlegend=True)
    st.plotly_chart(fig, width="stretch")

st.divider()

# ------------------------------
# AI Analysis - Error Categories
# ------------------------------
if "error_category" in df_f.columns and df_f["error_category"].notna().any():
    st.subheader("🔍 Analyse des problèmes")

    error_col1, error_col2 = st.columns([1.2, 1])

    with error_col1:
        st.markdown("### 🎯 Types d'erreurs identifiées")
        error_counts = df_f["error_category"].dropna().replace("", pd.NA).dropna().value_counts()

        # Create labels and colors
        error_labels = [get_error_category_label(cat) for cat in error_counts.index]
        error_colors = {get_error_category_label(cat): ERROR_CATEGORY_COLORS.get(cat, "#888888") for cat in error_counts.index}

        fig = px.pie(values=error_counts.values, names=error_labels, hole=0.4, color_discrete_map=error_colors)
        fig.update_layout(height=350, showlegend=True)
        st.plotly_chart(fig, width="stretch")

    with error_col2:
        st.markdown("### 📊 Répartition Retrieval vs Generation")

        # Group by retrieval vs generation issues
        def categorize_issue_type(cat):
            if cat == "retrieval_issue":
                return "🔍 Retrieval (recherche)"
            elif cat == "candidate_cut":
                return "📉 Agrégation (coupe candidats)"
            elif cat in ["selector_misunderstanding", "selector_wrong_priority"]:
                return "🎯 Selector (sélection)"
            elif cat in ["generator_hallucination", "generator_incomplete", "generator_wrong_interpretation"]:
                return "✍️ Generator (génération)"
            elif cat == "missing_document":
                return "📄 Document manquant"
            elif cat == "chunk_quality":
                return "📊 Qualité chunks"
            else:
                return "❓ Autre"

        df_with_type = df_f[df_f["error_category"].notna() & (df_f["error_category"] != "")].copy()
        df_with_type["issue_type"] = df_with_type["error_category"].apply(categorize_issue_type)
        type_counts = df_with_type["issue_type"].value_counts()

        type_colors = {
            "🔍 Retrieval (recherche)": "#00CC96",
            "📉 Agrégation (coupe candidats)": "#19D3F3",
            "🎯 Selector (sélection)": "#FECB52",
            "✍️ Generator (génération)": "#EF553B",
            "📄 Document manquant": "#636EFA",
            "📊 Qualité chunks": "#AB63FA",
            "❓ Autre": "#888888",
        }

        fig = px.bar(
            x=type_counts.index,
            y=type_counts.values,
            labels={"x": "Type de problème", "y": "Nombre"},
        )
        fig.update_traces(marker_color=[type_colors.get(t, "#888888") for t in type_counts.index])
        fig.update_layout(height=350, showlegend=False, xaxis_title="")
        st.plotly_chart(fig, width="stretch")

    st.divider()

# ------------------------------
# Reasons Analysis
# ------------------------------
st.subheader("📝 Analyse des raisons")

reason_col1, reason_col2 = st.columns(2)

with reason_col1:
    st.markdown("### 👍 **Raisons positives**")
    positive_reasons = []
    for reasons_list in df_f["reasons_positive_list"]:
        if isinstance(reasons_list, list):
            positive_reasons.extend(reasons_list)

    if positive_reasons:
        reasons_df = pd.DataFrame({"reason": positive_reasons})
        top_positive = reasons_df["reason"].value_counts().head(10)

        fig = px.bar(
            x=top_positive.values,
            y=top_positive.index,
            orientation="h",
            labels={"x": "Nombre", "y": "Raison"},
        )
        fig.update_traces(marker_color="#A8E6A1")
        fig.update_layout(showlegend=False, height=300, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Aucune raison positive enregistrée")

with reason_col2:
    st.markdown("### 👎 **Raisons négatives**")
    negative_reasons = []
    for reasons_list in df_f["reasons_negative_list"]:
        if isinstance(reasons_list, list):
            negative_reasons.extend(reasons_list)

    if negative_reasons:
        reasons_df = pd.DataFrame({"reason": negative_reasons})
        top_negative = reasons_df["reason"].value_counts().head(10)

        fig = px.bar(
            x=top_negative.values,
            y=top_negative.index,
            orientation="h",
            labels={"x": "Nombre", "y": "Raison"},
        )
        fig.update_traces(marker_color="#FFB3B3")
        fig.update_layout(showlegend=False, height=300, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Aucune raison négative enregistrée")

st.divider()

# ------------------------------
# Detailed Table
# ------------------------------
st.subheader("📋 Détails des feedbacks")
_applied_groups = ", ".join(get_group_label(g) for g in selected_groups)
st.caption(f"👥 Groupes : {_applied_groups} · 📅 Période : {applied_period_caption}")
feedback_export_df = pd.DataFrame()

if df_f.empty:
    st.info("Aucun feedback pour les filtres sélectionnés")
else:
    # Prepare display columns
    display_df = df_f.copy()

    # Format dist_after_rerank for display
    def format_dist(val):
        if pd.isna(val) or val is None:
            return "—"
        try:
            if isinstance(val, str):
                import json

                val = json.loads(val)
            if isinstance(val, dict):
                return ", ".join([f"{k}: {v}" for k, v in val.items()])
            return str(val)
        except:
            return str(val)[:50] if val else "—"

    if "dist_after_rerank" in display_df.columns:
        display_df["sources_dist"] = display_df["dist_after_rerank"].apply(format_dist)
    else:
        display_df["sources_dist"] = "—"

    # Format group for display
    display_df["groupe_display"] = display_df["user_group"].apply(get_group_label)

    # Format ministry for display (explicit column persisted by chat_logger;
    # historic rows without value show "Non renseigné" — no heuristic backfill)
    if "selected_ministry" in display_df.columns:
        display_df["ministere_display"] = display_df["selected_ministry"].apply(ministry_display_label)
    else:
        display_df["ministere_display"] = MINISTRY_NOT_SET_LABEL

    # Format error category for display
    if "error_category" in display_df.columns:
        display_df["error_label"] = display_df["error_category"].apply(get_error_category_label)
    else:
        display_df["error_label"] = "—"

    # Format AI reason (NO truncation - show full reason)
    if "ai_reason" in display_df.columns:
        display_df["ai_reason_display"] = display_df["ai_reason"].apply(lambda x: str(x) if x else "—")
    else:
        display_df["ai_reason_display"] = "—"

    # Format theme for display
    if "theme" in display_df.columns:
        display_df["theme_display"] = display_df["theme"].apply(get_theme_label)
    else:
        display_df["theme_display"] = "—"

    # Format beta_scope (Périmètre beta-test)
    if "beta_scope" in display_df.columns:
        display_df["beta_scope_display"] = display_df["beta_scope"].fillna("—")
    else:
        display_df["beta_scope_display"] = "—"

    # All columns - user can toggle visibility via Streamlit's native column menu
    # Note: "sources_dist" masquée par défaut (disponible dans les données si besoin)
    display_cols = [
        "ts",
        "label",
        "groupe_display",
        "ministere_display",
        "question",
        "answer",
        "reasons_positive",
        "reasons_negative",
        "comment",
        "error_label",
        "ai_reason_display",
        "theme_display",
        "beta_scope_display",
    ]

    # Filter to only existing columns
    display_cols = [c for c in display_cols if c in display_df.columns]

    col_config = {
        "ts": st.column_config.DatetimeColumn("📅 Date", format="DD/MM/YYYY HH:mm"),
        "label": st.column_config.TextColumn("⭐ Note", width="small"),
        "theme_display": st.column_config.TextColumn("🏷️ Thème", width="medium"),
        "beta_scope_display": st.column_config.TextColumn("📋 Périmètre beta-test", width="small"),
        "error_label": st.column_config.TextColumn("🤖 Erreur Label", width="medium"),
        "ai_reason_display": st.column_config.TextColumn("💡 Raison", width="large"),
        "groupe_display": st.column_config.TextColumn("👥 Groupe", width="small"),
        "ministere_display": st.column_config.TextColumn("🏛️ Ministère", width="small"),
        "question": st.column_config.TextColumn("❓ Question", width="medium"),
        "answer": st.column_config.TextColumn("💬 Réponse", width="medium"),
        "reasons_positive": st.column_config.TextColumn("👍 Positif", width="small"),
        "reasons_negative": st.column_config.TextColumn("👎 Négatif", width="small"),
        "comment": st.column_config.TextColumn("💭 Commentaire", width="medium"),
        "sources_dist": st.column_config.TextColumn("📚 Sources", width="medium"),
    }
    export_labels = {
        "ts": "📅 Date",
        "label": "⭐ Note",
        "theme_display": "🏷️ Thème",
        "beta_scope_display": "📋 Périmètre beta-test",
        "error_label": "🤖 Erreur Label",
        "ai_reason_display": "💡 Raison",
        "groupe_display": "👥 Groupe",
        "ministere_display": "🏛️ Ministère",
        "question": "❓ Question",
        "answer": "💬 Réponse",
        "reasons_positive": "👍 Positif",
        "reasons_negative": "👎 Négatif",
        "comment": "💭 Commentaire",
        "sources_dist": "📚 Sources",
    }
    feedback_display_df = display_df[display_cols].sort_values("ts", ascending=False).copy()
    if "ts" in feedback_display_df.columns:
        # Garder de vrais datetimes pour la grille : une chaîne "17/07/2026 …"
        # serait re-parsée mois/jour par le DataFrame (10/07 affiché 7 octobre).
        # Le format d'affichage vient de col_config (DatetimeColumn) ; le tz est
        # retiré après conversion Paris pour afficher l'heure locale telle quelle.
        feedback_display_df["ts"] = feedback_display_df["ts"].dt.tz_localize(None)
    feedback_export_df = feedback_display_df.copy()
    if "ts" in feedback_export_df.columns:
        feedback_export_df["ts"] = feedback_export_df["ts"].dt.strftime("%d/%m/%Y %H:%M")
    feedback_export_df = feedback_export_df.rename(columns={col: export_labels[col] for col in feedback_export_df.columns if col in export_labels})

    st.dataframe(feedback_display_df, width="stretch", hide_index=True, column_config=col_config, height=500)

st.divider()

# ------------------------------
# 🤖 Auto-analyse des feedbacks négatifs
# ------------------------------
_unanalyzed = 0
if "error_category" in df_f.columns and "stars" in df_f.columns:
    _mask_neg = df_f["stars"].notna() & (df_f["stars"] <= 2)
    _mask_no_cat = df_f["error_category"].isna() | (df_f["error_category"] == "")
    _unanalyzed = int((_mask_neg & _mask_no_cat).sum())

if _unanalyzed > 0 and "fb_auto_analysis_done" not in st.session_state:
    _banner = st.info(f"🤖 Analyse des {_unanalyzed} dernier(s) feedback(s) négatif(s) (≤3★)…")
    try:
        from assistant_rh_rag_pipeline.feedback_analyzer import run_batch_analysis

        stats = run_batch_analysis(limit=_unanalyzed)
        st.session_state["fb_auto_analysis_done"] = True
        _banner.empty()
        if stats.get("analyzed", 0) > 0:
            st.toast(f"✅ {stats['analyzed']} feedback(s) analysé(s) automatiquement", icon="✅")
            st.cache_data.clear()
            st.rerun()
    except Exception:
        st.session_state["fb_auto_analysis_done"] = True
        _banner.empty()

st.divider()

# ------------------------------
# Export
# ------------------------------
export_col1, export_col2, _ = st.columns([1, 1, 2])

with export_col1:
    try:
        feedback_xlsx = dataframe_to_xlsx_bytes(feedback_export_df, "Feedbacks")
    except RuntimeError as exc:
        st.warning(str(exc))
    else:
        st.download_button(
            "⬇️ Export Feedbacks (Excel)",
            feedback_xlsx,
            f"feedbacks_betatest_{datetime.now().strftime('%Y%m%d')}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

with export_col2:
    questions_export_df = df_q.copy()
    if "selected_ministry" in questions_export_df.columns:
        questions_export_df["selected_ministry"] = questions_export_df["selected_ministry"].apply(ministry_display_label)
        questions_export_df = questions_export_df.rename(columns={"selected_ministry": "Ministère"})
    if "ts" in questions_export_df.columns:
        questions_export_df["ts"] = (
            pd.to_datetime(questions_export_df["ts"], utc=True, errors="coerce").dt.tz_convert("Europe/Paris").dt.strftime("%d/%m/%Y %H:%M")
        )
    try:
        questions_xlsx = dataframe_to_xlsx_bytes(questions_export_df, "Questions")
    except RuntimeError as exc:
        st.warning(str(exc))
    else:
        st.download_button(
            "⬇️ Export Questions (Excel)",
            questions_xlsx,
            f"questions_betatest_{datetime.now().strftime('%Y%m%d')}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

# ------------------------------
# 👥 Usage & Engagement (données globales, hors filtre thème)
# ------------------------------
st.divider()
st.subheader("👥 Usage & Engagement")
st.caption("Vue globale sur la période sélectionnée (indépendante du filtre thème).")

if not df_q_usage.empty and "session_id" in df_q_usage.columns:
    # Metrics row
    u_col1, u_col2, u_col3, u_col4 = st.columns(4)
    n_questions = len(df_q_usage)
    n_users = df_q_usage["session_id"].nunique()
    avg_per_user = n_questions / n_users if n_users > 0 else 0
    n_with_feedback = df_q_usage["turn_id"].isin(df_f_usage["turn_id"]).sum() if "turn_id" in df_f_usage.columns else 0
    eval_rate_usage = 100 * n_with_feedback / n_questions if n_questions > 0 else 0

    with u_col1:
        st.metric("💬 Questions posées", n_questions)
    with u_col2:
        st.metric("👥 Utilisateurs uniques", n_users)
    with u_col3:
        st.metric("📊 Moy. questions / utilisateur", f"{avg_per_user:.1f}")
    with u_col4:
        st.metric("📝 Taux d'évaluation (usage)", f"{eval_rate_usage:.0f}%")

    # Top users + Weekly retention
    usage_col1, usage_col2 = st.columns([1, 1])

    with usage_col1:
        st.markdown("#### 🏆 Utilisateurs les plus actifs")
        user_counts = (
            df_q_usage.groupby("session_id")
            .agg(
                questions=("turn_id", "count"),
                groupe=("user_group", "first"),
            )
            .reset_index()
        )
        user_counts = user_counts.sort_values("questions", ascending=False).head(15)
        user_counts["utilisateur"] = user_counts["session_id"].apply(lambda x: str(x)[:12] + "…" if len(str(x)) > 12 else str(x))
        user_counts["groupe"] = user_counts["groupe"].apply(get_group_label)
        st.dataframe(
            user_counts[["utilisateur", "groupe", "questions"]],
            width="stretch",
            hide_index=True,
            column_config={
                "utilisateur": st.column_config.TextColumn("Session", width="medium"),
                "groupe": st.column_config.TextColumn("Groupe", width="small"),
                "questions": st.column_config.NumberColumn("Questions", width="small"),
            },
            height=320,
        )

    with usage_col2:
        st.markdown("#### 📅 Rétention hebdomadaire")
        if "date" in df_q_usage.columns:
            df_w = df_q_usage.copy()
            df_w["week"] = pd.to_datetime(df_w["date"]).dt.isocalendar().week
            df_w["year"] = pd.to_datetime(df_w["date"]).dt.isocalendar().year
            weekly_users = df_w.groupby(["year", "week"])["session_id"].nunique().reset_index(name="utilisateurs")
            weekly_questions = df_w.groupby(["year", "week"])["turn_id"].count().reset_index(name="questions")
            weekly = weekly_users.merge(weekly_questions, on=["year", "week"]).sort_values(["year", "week"])
            weekly["semaine"] = weekly.apply(lambda r: f"S{r['week']}" if r["year"] == 2026 else f"{r['year']}-S{r['week']}", axis=1)

            fig_ret = go.Figure()
            fig_ret.add_trace(go.Bar(name="Utilisateurs actifs", x=weekly["semaine"], y=weekly["utilisateurs"], marker_color="#636EFA"))
            fig_ret.add_trace(
                go.Scatter(name="Questions", x=weekly["semaine"], y=weekly["questions"], yaxis="y2", mode="lines+markers", line=dict(color="#00CC96"))
            )
            fig_ret.update_layout(
                yaxis=dict(title="Utilisateurs"),
                yaxis2=dict(title="Questions", overlaying="y", side="right"),
                height=320,
                legend=dict(orientation="h", y=1.1),
                margin=dict(t=40),
            )
            st.plotly_chart(fig_ret, width="stretch")
        else:
            st.info("Pas de données temporelles pour la rétention.")

    # Adoption formulaire d'évaluation
    st.markdown("#### 📋 Adoption du formulaire d'évaluation")
    eval_col1, eval_col2, eval_col3 = st.columns(3)
    n_users_evaluated = df_f_usage["session_id"].nunique() if "session_id" in df_f_usage.columns else 0
    pct_users_evaluated = 100 * n_users_evaluated / n_users if n_users > 0 else 0

    with eval_col1:
        st.metric("Utilisateurs ayant évalué au moins 1 réponse", f"{n_users_evaluated} / {n_users}", f"{pct_users_evaluated:.0f}%")
    with eval_col2:
        st.metric("Questions évaluées", n_with_feedback, f"{eval_rate_usage:.0f}% des questions")
    with eval_col3:
        if n_users_evaluated > 0 and len(df_f_usage) > 0:
            avg_eval_per_user = len(df_f_usage) / n_users_evaluated
            st.metric("Moy. évaluations / utilisateur ayant évalué", f"{avg_eval_per_user:.1f}", "")
        else:
            st.metric("Moy. évaluations / utilisateur", "—", "")

else:
    st.info("Aucune donnée d'usage disponible (session_id manquant).")
