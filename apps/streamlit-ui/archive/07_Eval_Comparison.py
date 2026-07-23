"""
Eval Comparison - Compare RAGAS metrics across RAG configurations
"""
import json
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import text

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.db_utils import get_engine

load_dotenv()

require_admin()
show_admin_badge()

st.set_page_config(
    page_title="Eval Comparison",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Eval Comparison")
st.markdown("Comparer les métriques RAGAS entre configurations RAG (V1, V2, V3, DRY...)")


# =============================================================================
# Data Loading
# =============================================================================

METRIC_COLS = [
    "faithfulness", "answer_relevancy", "context_precision",
    "context_recall", "answer_correctness", "answer_similarity",
]

METRIC_LABELS = {
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer Relevancy",
    "context_precision": "Context Precision",
    "context_recall": "Context Recall",
    "answer_correctness": "Answer Correctness",
    "answer_similarity": "Answer Similarity",
}

# Metrics that require context vs answer-only
CONTEXT_METRICS = {"faithfulness", "context_precision", "context_recall"}
ANSWER_METRICS = {"answer_relevancy", "answer_correctness", "answer_similarity"}

# Judge model suffix mapping
JUDGE_SUFFIXES = {
    "gpt-4o-mini (par défaut)": "",
    "gpt-4.1-mini": "_41mini",
}


@st.cache_data(ttl=120)
def load_configs_summary():
    """Load summary of available configs with counts and metric availability."""
    engine = get_engine()
    if not engine:
        return pd.DataFrame()
    query = text("""
        SELECT
            gr.config_name,
            COUNT(*) as run_count,
            COUNT(gr.metrics) FILTER (WHERE gr.metrics IS NOT NULL AND gr.metrics != '{}'::jsonb) as with_metrics,
            MIN(gr.run_timestamp) as first_run,
            MAX(gr.run_timestamp) as last_run
        FROM goldset_runs gr
        GROUP BY gr.config_name
        ORDER BY run_count DESC
    """)
    return pd.read_sql(query, engine)


@st.cache_data(ttl=120)
def load_runs_with_metrics(config_names: tuple, goldset_names: tuple = None, themes: tuple = None, tags: tuple = None, judge_suffix: str = ""):
    """Load all runs with metrics for selected configs.
    
    Args:
        tags: tuple of tags to filter by (all must be present on the question).
        judge_suffix: "" for 4o-mini (default keys), "_41mini" for 4.1-mini keys.
                      Falls back to default keys if suffixed key not found.
    """
    engine = get_engine()
    if not engine:
        return pd.DataFrame()

    where_clauses = ["gr.config_name = ANY(:configs)", "gr.response IS NOT NULL"]
    params = {"configs": list(config_names)}

    if goldset_names:
        where_clauses.append("gq.goldset_name = ANY(:goldsets)")
        params["goldsets"] = list(goldset_names)
    if themes:
        where_clauses.append("gq.theme = ANY(:themes)")
        params["themes"] = list(themes)
    if tags:
        for i, tag in enumerate(tags):
            where_clauses.append(f"gq.tags @> ARRAY[:tag_{i}]")
            params[f"tag_{i}"] = tag

    where_sql = " AND ".join(where_clauses)

    query = text(f"""
        SELECT
            gr.id as run_id,
            gr.question_id,
            gq.question,
            gq.theme,
            gq.goldset_name,
            gq.gold_answer,
            gr.config_name,
            gr.response,
            gr.metrics,
            gr.retrieval_time_ms,
            gr.generation_time_ms,
            gr.run_timestamp
        FROM goldset_runs gr
        JOIN goldset_questions_v2 gq ON gr.question_id = gq.id
        WHERE {where_sql}
        ORDER BY gr.question_id, gr.config_name
    """)
    df = pd.read_sql(query, engine, params=params)

    # Extract metrics from JSONB — with judge suffix support + fallback
    def _get_metric(m, col, suffix):
        """Try suffixed key first, fall back to default key."""
        val = _extract_metric(m, f"{col}{suffix}")
        if val is not None:
            return val
        return _extract_metric(m, col)

    for col in METRIC_COLS:
        if judge_suffix:
            df[col] = df["metrics"].apply(lambda m, c=col, s=judge_suffix: _get_metric(m, c, s))
        else:
            df[col] = df["metrics"].apply(lambda m, c=col: _extract_metric(m, c))

    df["has_gold"] = df["gold_answer"].apply(lambda x: bool(x and str(x).strip()))

    # Extract refusal and context flags from metrics JSONB
    def _get_bool_metric(metrics_val, key, default=False):
        if not metrics_val:
            return default
        if isinstance(metrics_val, str):
            try:
                metrics_val = json.loads(metrics_val)
            except (json.JSONDecodeError, TypeError):
                return default
        if isinstance(metrics_val, dict):
            val = metrics_val.get(key)
            if val is not None:
                return bool(val)
        return default

    df["is_refusal"] = df["metrics"].apply(lambda m: _get_bool_metric(m, "is_refusal", default=False))
    df["has_context"] = df["metrics"].apply(lambda m: _get_bool_metric(m, "has_context", default=True))

    return df


@st.cache_data(ttl=120)
def load_goldsets_and_themes():
    """Load available goldsets, themes, and tags."""
    engine = get_engine()
    if not engine:
        return [], [], []
    q_goldsets = text("SELECT DISTINCT goldset_name FROM goldset_questions_v2 WHERE goldset_name IS NOT NULL ORDER BY goldset_name")
    q_themes = text("SELECT DISTINCT theme FROM goldset_questions_v2 WHERE theme IS NOT NULL ORDER BY theme")
    q_tags = text("SELECT DISTINCT unnest(tags) as tag FROM goldset_questions_v2 WHERE tags IS NOT NULL ORDER BY tag")
    goldsets = pd.read_sql(q_goldsets, engine)["goldset_name"].tolist()
    themes = pd.read_sql(q_themes, engine)["theme"].tolist()
    tags = pd.read_sql(q_tags, engine)["tag"].tolist()
    return goldsets, themes, tags


def _extract_metric(metrics_val, key):
    """Extract a metric value from JSONB."""
    if not metrics_val:
        return None
    if isinstance(metrics_val, str):
        try:
            metrics_val = json.loads(metrics_val)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(metrics_val, dict):
        val = metrics_val.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None


# =============================================================================
# Chart Helpers
# =============================================================================

CONFIG_COLORS = {
    "v3_prod": "#2196F3",
    "v1_prod": "#FF9800",
    "v2_prod": "#4CAF50",
    "dry_no_ctx": "#9E9E9E",
    "v3_reuse_mistral_med": "#9C27B0",
    "v3_mistralmed": "#E91E63",
    "v3_albertlrg": "#00BCD4",
    "v3_optim": "#FF5722",
}


def get_color(config_name):
    return CONFIG_COLORS.get(config_name, "#607D8B")


def create_metrics_bar_chart(summary_df, metrics_to_show, title="Comparaison des métriques RAGAS"):
    """Grouped bar chart comparing metrics across configs."""
    plot_data = []
    for _, row in summary_df.iterrows():
        for m in metrics_to_show:
            val = row.get(m)
            if pd.notna(val):
                plot_data.append({
                    "Config": row["config_name"],
                    "Metric": METRIC_LABELS.get(m, m),
                    "Score": val,
                })
    if not plot_data:
        return None
    df_plot = pd.DataFrame(plot_data)
    fig = px.bar(
        df_plot, x="Metric", y="Score", color="Config",
        barmode="group", title=title,
        color_discrete_map={c: get_color(c) for c in df_plot["Config"].unique()},
    )
    fig.update_layout(yaxis_range=[0, 1], yaxis_title="Score", xaxis_title="",
                      legend_title="Config", height=450)
    return fig


def create_radar_chart(summary_df, metrics_to_show):
    """Radar chart comparing configs across metrics."""
    fig = go.Figure()
    for _, row in summary_df.iterrows():
        values = [row.get(m, 0) or 0 for m in metrics_to_show]
        values.append(values[0])  # Close the polygon
        labels = [METRIC_LABELS.get(m, m) for m in metrics_to_show]
        labels.append(labels[0])
        fig.add_trace(go.Scatterpolar(
            r=values, theta=labels,
            fill="toself", name=row["config_name"],
            line_color=get_color(row["config_name"]),
            opacity=0.7,
        ))
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
        showlegend=True, height=500, title="Radar des métriques",
    )
    return fig


def create_distribution_box(df, metric, title=None):
    """Box plot for a metric across configs."""
    df_valid = df.dropna(subset=[metric])
    if df_valid.empty:
        return None
    fig = px.box(
        df_valid, x="config_name", y=metric,
        color="config_name", title=title or METRIC_LABELS.get(metric, metric),
        color_discrete_map={c: get_color(c) for c in df_valid["config_name"].unique()},
        points="outliers",
    )
    fig.update_layout(yaxis_range=[0, 1.05], yaxis_title="Score",
                      xaxis_title="", showlegend=False, height=400)
    return fig


def create_per_question_heatmap(df_pivot, metric):
    """Heatmap of metric scores per question x config."""
    fig = px.imshow(
        df_pivot.values,
        x=df_pivot.columns.tolist(),
        y=[f"Q{i}" for i in df_pivot.index],
        color_continuous_scale="RdYlGn",
        zmin=0, zmax=1,
        aspect="auto",
        title=f"{METRIC_LABELS.get(metric, metric)} par question",
    )
    fig.update_layout(height=max(400, len(df_pivot) * 18))
    return fig


# =============================================================================
# Sidebar
# =============================================================================

engine = get_engine()
if not engine:
    st.error("Base de données non connectée. Vérifiez `APP_POSTGRES_DSN` ou la DSN historique active.")
    st.stop()

# Load config summary
configs_summary = load_configs_summary()
if configs_summary.empty:
    st.warning("Aucun run trouvé dans goldset_runs.")
    st.stop()

# Load goldsets and themes
all_goldsets, all_themes, all_tags = load_goldsets_and_themes()

st.sidebar.header("Filtres")

# Config selector
config_options = configs_summary["config_name"].tolist()
default_configs = [c for c in ["v3_optim", "v3_prod", "v1_prod", "v2_prod", "dry_no_ctx", "v3_reuse_mistral_med"] if c in config_options]
selected_configs = st.sidebar.multiselect(
    "Configurations",
    options=config_options,
    default=default_configs,
    help="Sélectionnez les configs à comparer",
)

if not selected_configs:
    st.info("Sélectionnez au moins une configuration dans la sidebar.")
    st.stop()

# Goldset filter
selected_goldsets = st.sidebar.multiselect(
    "Goldsets",
    options=all_goldsets,
    default=[],
    help="Filtrer par goldset (vide = tous)",
)

# Theme filter
selected_themes = st.sidebar.multiselect(
    "Thèmes",
    options=all_themes,
    default=[],
    help="Filtrer par thème (vide = tous)",
)

# Tags filter
selected_tags = st.sidebar.multiselect(
    "Tags",
    options=all_tags,
    default=[],
    help="Filtrer par tag (ex: golden_beta). Vide = toutes les questions.",
)

# Gold answer filter
gold_filter = st.sidebar.radio(
    "Questions avec gold_answer",
    options=["Toutes", "Avec gold_answer uniquement", "Sans gold_answer uniquement"],
    index=0,
)

st.sidebar.divider()

# Judge model toggle
st.sidebar.subheader("Modèle Judge RAGAS")
judge_model = st.sidebar.radio(
    "Métriques à afficher",
    options=list(JUDGE_SUFFIXES.keys()),
    index=0,
    help="gpt-4o-mini = métriques par défaut. gpt-4.1-mini = recalculées avec le nouveau judge (fallback sur 4o-mini si indisponible).",
)
judge_suffix = JUDGE_SUFFIXES[judge_model]
if judge_suffix:
    st.sidebar.caption(f"Clés: `faithfulness{judge_suffix}`, `answer_correctness{judge_suffix}`, ...")

# Refusal filter
st.sidebar.subheader("Gestion des refus")
refusal_filter = st.sidebar.radio(
    "Réponses de type refus",
    options=["Inclure tout", "Exclure les refus", "Refus uniquement"],
    index=0,
    help="Un 'refus' = réponse du type 'je ne sais pas', 'je n'ai pas trouvé'... Exclure les refus montre la qualité des réponses effectives.",
)

st.sidebar.divider()

# Show config summary in sidebar
st.sidebar.subheader("Configs sélectionnées")
for _, row in configs_summary[configs_summary["config_name"].isin(selected_configs)].iterrows():
    st.sidebar.markdown(
        f"**{row['config_name']}** — {row['run_count']} runs, {row['with_metrics']} avec métriques"
    )


# =============================================================================
# Load Data
# =============================================================================

df = load_runs_with_metrics(
    config_names=tuple(selected_configs),
    goldset_names=tuple(selected_goldsets) if selected_goldsets else None,
    themes=tuple(selected_themes) if selected_themes else None,
    tags=tuple(selected_tags) if selected_tags else None,
    judge_suffix=judge_suffix,
)

if df.empty:
    st.warning("Aucun run trouvé avec ces filtres.")
    st.stop()

# Apply gold_answer filter
if gold_filter == "Avec gold_answer uniquement":
    df = df[df["has_gold"]].reset_index(drop=True)
elif gold_filter == "Sans gold_answer uniquement":
    df = df[~df["has_gold"]].reset_index(drop=True)

if df.empty:
    st.warning("Aucun run après filtrage.")
    st.stop()

# Store full df (before refusal filter) for refusal analysis tab
df_all = df.copy()

# Apply refusal filter
n_refusals = df["is_refusal"].sum()
if refusal_filter == "Exclure les refus":
    df = df[~df["is_refusal"]].reset_index(drop=True)
elif refusal_filter == "Refus uniquement":
    df = df[df["is_refusal"]].reset_index(drop=True)

if df.empty:
    st.warning("Aucun run après filtrage.")
    st.stop()

_judge_label = judge_model.split(" (")[0]
_refusal_info = f" — {n_refusals} refus détectés" if n_refusals > 0 else ""
_filter_info = " — **refus exclus**" if refusal_filter == "Exclure les refus" else (" — **refus uniquement**" if refusal_filter == "Refus uniquement" else "")
_tags_info = f" — tags: {', '.join(selected_tags)}" if selected_tags else ""
st.caption(f"{len(df)} runs chargés — {df['question_id'].nunique()} questions — {df['config_name'].nunique()} configs — Judge: **{_judge_label}**{_refusal_info}{_filter_info}{_tags_info}")


# =============================================================================
# Compute Summary
# =============================================================================

def compute_summary(df):
    """Compute mean metrics per config, including refusal rate."""
    rows = []
    for cfg, grp in df.groupby("config_name"):
        n_refusals = grp["is_refusal"].sum() if "is_refusal" in grp.columns else 0
        row = {
            "config_name": cfg,
            "n_runs": len(grp),
            "refusal_rate": round(n_refusals / len(grp) * 100, 1) if len(grp) > 0 else 0,
        }
        for m in METRIC_COLS:
            vals = grp[m].dropna()
            row[m] = vals.mean() if len(vals) > 0 else None
            row[f"{m}_n"] = len(vals)
            row[f"{m}_std"] = vals.std() if len(vals) > 1 else None
        rows.append(row)
    return pd.DataFrame(rows)


summary = compute_summary(df)

# Determine which metrics have data
metrics_with_data = [m for m in METRIC_COLS if summary[m].notna().any()]


# =============================================================================
# Tabs
# =============================================================================

tab_overview, tab_refusal, tab_compare, tab_distrib, tab_questions = st.tabs([
    "Vue d'ensemble",
    "Analyse des Refus",
    "Comparaison par goldset",
    "Distributions",
    "Exploration par question",
])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1: Overview
# ─────────────────────────────────────────────────────────────────────────────
with tab_overview:
    st.subheader("Résumé des métriques par configuration")

    # Summary table with highlighting
    display_cols = ["config_name", "n_runs", "refusal_rate"] + metrics_with_data
    display_df = summary[display_cols].copy()

    # Format metrics
    for m in metrics_with_data:
        display_df[m] = display_df[m].apply(lambda x: round(x, 3) if pd.notna(x) else None)

    # Rename columns for display
    rename_map = {"config_name": "Config", "n_runs": "Runs", "refusal_rate": "Refus (%)"}
    rename_map.update({m: METRIC_LABELS.get(m, m) for m in metrics_with_data})
    display_df = display_df.rename(columns=rename_map)

    # Highlight best values
    metric_display_cols = [METRIC_LABELS.get(m, m) for m in metrics_with_data]

    def highlight_best(s):
        if s.name not in metric_display_cols:
            return [""] * len(s)
        numeric = pd.to_numeric(s, errors="coerce")
        is_best = numeric == numeric.max()
        return ["background-color: #c8e6c9; font-weight: bold" if v else "" for v in is_best]

    st.dataframe(
        display_df.style.apply(highlight_best, axis=0),
        width="stretch",
        hide_index=True,
    )

    # Sample counts per metric
    with st.expander("Nombre d'observations par métrique"):
        count_cols = ["config_name"] + [f"{m}_n" for m in metrics_with_data]
        count_df = summary[count_cols].copy()
        count_df = count_df.rename(columns={"config_name": "Config"})
        count_df = count_df.rename(columns={f"{m}_n": METRIC_LABELS.get(m, m) for m in metrics_with_data})
        st.dataframe(count_df, width="stretch", hide_index=True)

    # Charts
    col_bar, col_radar = st.columns(2)

    with col_bar:
        fig_bar = create_metrics_bar_chart(summary, metrics_with_data)
        if fig_bar:
            st.plotly_chart(fig_bar, width="stretch")

    with col_radar:
        # Radar only for metrics present in all configs
        radar_metrics = [m for m in metrics_with_data if summary[m].notna().all()]
        if len(radar_metrics) >= 3:
            fig_radar = create_radar_chart(summary, radar_metrics)
            st.plotly_chart(fig_radar, width="stretch")
        else:
            st.info("Le radar nécessite au moins 3 métriques communes à toutes les configs.")

    # Key metrics as KPI cards
    st.divider()
    st.subheader("Meilleures configs par métrique")
    kpi_cols = st.columns(min(len(metrics_with_data), 6))
    for i, m in enumerate(metrics_with_data):
        with kpi_cols[i % len(kpi_cols)]:
            valid = summary.dropna(subset=[m])
            if not valid.empty:
                best_row = valid.loc[valid[m].idxmax()]
                st.metric(
                    label=METRIC_LABELS.get(m, m),
                    value=f"{best_row[m]:.3f}",
                    delta=best_row["config_name"],
                )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2: Refusal Analysis
# ─────────────────────────────────────────────────────────────────────────────
with tab_refusal:
    st.subheader("Analyse des Refus / Abstentions")
    st.markdown("""
    Un **refus** = le pipeline répond "je ne sais pas", "je n'ai pas trouvé", etc.
    C'est souvent le comportement **souhaité** pour éviter les hallucinations.
    Les métriques RAGAS pénalisent injustement ces refus (answer_correctness et answer_relevancy proches de 0).
    """)

    # Use df_all (before refusal filter) for this analysis
    _df = df_all.copy()

    # ── Section 1: Refusal rate per config ──
    st.markdown("### Taux de refus par configuration")

    refusal_stats = []
    for cfg, grp in _df.groupby("config_name"):
        n_total = len(grp)
        n_refusal = grp["is_refusal"].sum()
        n_no_ctx = (~grp["has_context"]).sum()
        refusal_stats.append({
            "Config": cfg,
            "Total": n_total,
            "Refus": int(n_refusal),
            "Taux refus (%)": round(n_refusal / n_total * 100, 1) if n_total > 0 else 0,
            "Sans contexte": int(n_no_ctx),
        })
    refusal_df = pd.DataFrame(refusal_stats).sort_values("Taux refus (%)", ascending=False)

    col_table, col_chart = st.columns([1, 2])
    with col_table:
        st.dataframe(refusal_df, width="stretch", hide_index=True)
    with col_chart:
        fig_refusal = px.bar(
            refusal_df, x="Config", y="Taux refus (%)",
            title="Taux de refus par configuration",
            text="Taux refus (%)",
            color="Taux refus (%)",
            color_continuous_scale=["#2ecc71", "#e74c3c"],
        )
        fig_refusal.update_traces(textposition="outside", texttemplate="%{text:.1f}%")
        fig_refusal.update_layout(showlegend=False, coloraxis_showscale=False)
        st.plotly_chart(fig_refusal, width="stretch")

    # ── Section 2: Metrics — All vs Answered-only ──
    st.markdown("### Comparaison : Toutes les questions vs Réponses effectives")
    st.markdown("""
    *Quand on exclut les refus, la qualité des réponses effectives des pipelines RAG
    apparaît plus clairement.*
    """)

    # Compute summary for all and answered-only
    def _compute_means(data, label):
        rows = []
        for cfg, grp in data.groupby("config_name"):
            row = {"Config": cfg, "Vue": label, "N": len(grp)}
            for m in METRIC_COLS:
                vals = grp[m].dropna()
                row[METRIC_LABELS.get(m, m)] = round(vals.mean(), 3) if len(vals) > 0 else None
            rows.append(row)
        return pd.DataFrame(rows)

    df_answered = _df[~_df["is_refusal"]]
    summary_all = _compute_means(_df, "Toutes")
    summary_answered = _compute_means(df_answered, "Sans refus")

    comparison_df = pd.concat([summary_all, summary_answered], ignore_index=True)

    # Show side-by-side for key metrics
    key_metrics = ["Faithfulness", "Answer Relevancy", "Answer Correctness", "Answer Similarity"]
    available_metrics = [m for m in key_metrics if m in comparison_df.columns and comparison_df[m].notna().any()]

    if available_metrics:
        for metric in available_metrics:
            fig_cmp = px.bar(
                comparison_df[comparison_df[metric].notna()],
                x="Config", y=metric, color="Vue",
                barmode="group",
                title=f"{metric} — Toutes vs Sans refus",
            )
            fig_cmp.update_layout(yaxis_range=[0, 1])
            st.plotly_chart(fig_cmp, width="stretch")

    # ── Section 3: Faithfulness caveat ──
    st.markdown("### Caveat : Faithfulness sans contexte")
    st.info("""
    **Faithfulness** mesure si les affirmations de la réponse sont supportées par le contexte fourni.
    Quand le contexte est **vide** (dry_no_ctx, ou refus avant retrieval), cette métrique est **trompeuse** :
    RAGAS peut donner un score élevé car il n'y a rien à contredire.

    **Recommandation** : ignorer Faithfulness pour les configurations dry et les runs sans contexte.
    Utilisez plutôt Answer Correctness et Answer Relevancy pour comparer dry vs RAG.
    """)

    # Show faithfulness breakdown: with context vs without
    df_with_ctx = _df[_df["has_context"]]
    df_no_ctx = _df[~_df["has_context"]]

    if len(df_no_ctx) > 0 and len(df_with_ctx) > 0:
        faith_data = []
        for cfg, grp in _df.groupby("config_name"):
            ctx_vals = grp[grp["has_context"]]["faithfulness"].dropna()
            no_ctx_vals = grp[~grp["has_context"]]["faithfulness"].dropna()
            if len(ctx_vals) > 0:
                faith_data.append({"Config": cfg, "Contexte": "Avec contexte", "Faithfulness": round(ctx_vals.mean(), 3), "N": len(ctx_vals)})
            if len(no_ctx_vals) > 0:
                faith_data.append({"Config": cfg, "Contexte": "Sans contexte", "Faithfulness": round(no_ctx_vals.mean(), 3), "N": len(no_ctx_vals)})

        if faith_data:
            faith_df = pd.DataFrame(faith_data)
            fig_faith = px.bar(
                faith_df, x="Config", y="Faithfulness", color="Contexte",
                barmode="group",
                title="Faithfulness : avec vs sans contexte",
            )
            fig_faith.update_layout(yaxis_range=[0, 1])
            st.plotly_chart(fig_faith, width="stretch")
            st.caption("Les scores de Faithfulness sans contexte sont artificiellement hauts et doivent être ignorés.")

    # ── Section 4: Refusal rate by goldset ──
    st.markdown("### Taux de refus par goldset")
    refusal_by_gs = []
    for (gs, cfg), grp in _df.groupby(["goldset_name", "config_name"]):
        n = len(grp)
        nr = grp["is_refusal"].sum()
        refusal_by_gs.append({
            "Goldset": gs, "Config": cfg,
            "Total": n, "Refus": int(nr),
            "Taux (%)": round(nr / n * 100, 1) if n > 0 else 0,
        })
    refusal_gs_df = pd.DataFrame(refusal_by_gs)

    if not refusal_gs_df.empty:
        fig_gs = px.bar(
            refusal_gs_df, x="Config", y="Taux (%)",
            color="Config", facet_col="Goldset", facet_col_wrap=3,
            title="Taux de refus par goldset et configuration",
        )
        fig_gs.update_layout(showlegend=False)
        st.plotly_chart(fig_gs, width="stretch")

        with st.expander("Tableau détaillé"):
            st.dataframe(refusal_gs_df.sort_values(["Goldset", "Taux (%)"], ascending=[True, False]),
                         width="stretch", hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3: Comparison by goldset
# ─────────────────────────────────────────────────────────────────────────────
with tab_compare:
    st.subheader("Comparaison par goldset")

    # Group by goldset
    goldsets_in_data = sorted(df["goldset_name"].dropna().unique())

    if not goldsets_in_data:
        st.warning("Aucun goldset trouvé.")
    else:
        selected_gs = st.selectbox("Goldset", goldsets_in_data, index=0)
        df_gs = df[df["goldset_name"] == selected_gs]

        if df_gs.empty:
            st.warning("Aucun run pour ce goldset.")
        else:
            summary_gs = compute_summary(df_gs)
            metrics_gs = [m for m in METRIC_COLS if summary_gs[m].notna().any()]

            st.caption(f"{len(df_gs)} runs — {df_gs['question_id'].nunique()} questions — {df_gs['config_name'].nunique()} configs")

            # Summary table
            disp = summary_gs[["config_name", "n_runs"] + metrics_gs].copy()
            for m in metrics_gs:
                disp[m] = disp[m].apply(lambda x: round(x, 3) if pd.notna(x) else None)
            disp = disp.rename(columns={"config_name": "Config", "n_runs": "Runs"})
            disp = disp.rename(columns={m: METRIC_LABELS.get(m, m) for m in metrics_gs})
            st.dataframe(disp, width="stretch", hide_index=True)

            # Bar chart for this goldset
            fig = create_metrics_bar_chart(summary_gs, metrics_gs, title=f"Métriques — {selected_gs}")
            if fig:
                st.plotly_chart(fig, width="stretch")

            # By theme within goldset
            themes_in_gs = sorted(df_gs["theme"].dropna().unique())
            if themes_in_gs:
                with st.expander(f"Détail par thème ({len(themes_in_gs)} thèmes)"):
                    theme_data = []
                    for theme in themes_in_gs:
                        df_t = df_gs[df_gs["theme"] == theme]
                        for cfg, grp in df_t.groupby("config_name"):
                            row = {"theme": theme, "config_name": cfg, "n": len(grp)}
                            for m in metrics_gs:
                                vals = grp[m].dropna()
                                row[m] = vals.mean() if len(vals) > 0 else None
                            theme_data.append(row)
                    df_theme = pd.DataFrame(theme_data)
                    if not df_theme.empty:
                        for m in metrics_gs[:3]:  # Top 3 metrics
                            fig_t = px.bar(
                                df_theme.dropna(subset=[m]),
                                x="theme", y=m, color="config_name",
                                barmode="group",
                                title=f"{METRIC_LABELS.get(m, m)} par thème",
                                color_discrete_map={c: get_color(c) for c in df_theme["config_name"].unique()},
                            )
                            fig_t.update_layout(yaxis_range=[0, 1], height=400)
                            st.plotly_chart(fig_t, width="stretch")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4: Distributions
# ─────────────────────────────────────────────────────────────────────────────
with tab_distrib:
    st.subheader("Distribution des scores")

    dist_metrics = st.multiselect(
        "Métriques à afficher",
        options=metrics_with_data,
        default=metrics_with_data[:4],
        format_func=lambda m: METRIC_LABELS.get(m, m),
    )

    if dist_metrics:
        cols = st.columns(min(len(dist_metrics), 2))
        for i, m in enumerate(dist_metrics):
            with cols[i % 2]:
                fig = create_distribution_box(df, m)
                if fig:
                    st.plotly_chart(fig, width="stretch")
                else:
                    st.info(f"Pas de données pour {METRIC_LABELS.get(m, m)}")

    # Histogram overlay
    st.divider()
    st.subheader("Histogrammes superposés")
    hist_metric = st.selectbox(
        "Métrique",
        options=metrics_with_data,
        format_func=lambda m: METRIC_LABELS.get(m, m),
        key="hist_metric",
    )
    df_hist = df.dropna(subset=[hist_metric])
    if not df_hist.empty:
        fig_hist = px.histogram(
            df_hist, x=hist_metric, color="config_name",
            barmode="overlay", nbins=30, opacity=0.6,
            title=f"Distribution — {METRIC_LABELS.get(hist_metric, hist_metric)}",
            color_discrete_map={c: get_color(c) for c in df_hist["config_name"].unique()},
        )
        fig_hist.update_layout(xaxis_range=[0, 1.05], height=400, xaxis_title="Score", yaxis_title="Count")
        st.plotly_chart(fig_hist, width="stretch")

    # Scatter: metric A vs metric B
    st.divider()
    st.subheader("Scatter: métrique vs métrique")
    if len(metrics_with_data) >= 2:
        sc1, sc2 = st.columns(2)
        with sc1:
            scatter_x = st.selectbox("Axe X", metrics_with_data, index=0,
                                     format_func=lambda m: METRIC_LABELS.get(m, m), key="sc_x")
        with sc2:
            scatter_y = st.selectbox("Axe Y", metrics_with_data,
                                     index=min(1, len(metrics_with_data) - 1),
                                     format_func=lambda m: METRIC_LABELS.get(m, m), key="sc_y")
        df_sc = df.dropna(subset=[scatter_x, scatter_y])
        if not df_sc.empty:
            fig_sc = px.scatter(
                df_sc, x=scatter_x, y=scatter_y, color="config_name",
                hover_data=["question_id", "question"],
                opacity=0.6,
                title=f"{METRIC_LABELS[scatter_x]} vs {METRIC_LABELS[scatter_y]}",
                color_discrete_map={c: get_color(c) for c in df_sc["config_name"].unique()},
            )
            fig_sc.update_layout(xaxis_range=[0, 1.05], yaxis_range=[0, 1.05], height=500)
            st.plotly_chart(fig_sc, width="stretch")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4: Per-question exploration
# ─────────────────────────────────────────────────────────────────────────────
with tab_questions:
    st.subheader("Exploration par question")

    # Questions that appear in multiple configs
    q_counts = df.groupby("question_id")["config_name"].nunique().reset_index()
    q_counts.columns = ["question_id", "n_configs"]
    multi_config_qs = q_counts[q_counts["n_configs"] > 1]["question_id"].tolist()

    questions_df = df[df["question_id"].isin(multi_config_qs)][
        ["question_id", "question", "goldset_name", "theme", "has_gold"]
    ].drop_duplicates(subset=["question_id"]).sort_values("question_id")

    if questions_df.empty:
        st.info("Sélectionnez au moins 2 configs avec des questions communes.")
        st.stop()

    st.caption(f"{len(questions_df)} questions présentes dans au moins 2 configs")

    # Question search
    search_q = st.text_input("Rechercher une question", placeholder="Tapez un mot-clé...")
    if search_q:
        mask = questions_df["question"].str.contains(search_q, case=False, na=False)
        questions_df = questions_df[mask]

    # Pagination
    page_size = 10
    n_pages = max(1, (len(questions_df) + page_size - 1) // page_size)
    page = st.number_input("Page", min_value=1, max_value=n_pages, value=1)
    start_idx = (page - 1) * page_size
    page_questions = questions_df.iloc[start_idx:start_idx + page_size]

    for _, q_row in page_questions.iterrows():
        qid = q_row["question_id"]
        question_text = q_row["question"]
        gold = q_row.get("has_gold", False)

        with st.expander(
            f"{'🏆' if gold else '❓'} Q{qid} — {question_text[:100]}{'...' if len(question_text) > 100 else ''}"
        ):
            runs_q = df[df["question_id"] == qid].sort_values("config_name")

            # Metric comparison table for this question
            q_metrics = []
            for _, r in runs_q.iterrows():
                row = {"Config": r["config_name"]}
                for m in METRIC_COLS:
                    val = r[m]
                    row[METRIC_LABELS.get(m, m)] = round(val, 3) if pd.notna(val) else None
                q_metrics.append(row)
            df_qm = pd.DataFrame(q_metrics)
            st.dataframe(df_qm, width="stretch", hide_index=True)

            # Show responses side by side
            resp_cols = st.columns(len(runs_q))
            for i, (_, r) in enumerate(runs_q.iterrows()):
                with resp_cols[i]:
                    st.markdown(f"**{r['config_name']}**")
                    response = r.get("response", "") or ""
                    st.text_area(
                        f"Réponse {r['config_name']}",
                        value=response[:2000] + ("..." if len(response) > 2000 else ""),
                        height=250,
                        disabled=True,
                        key=f"resp_{qid}_{r['config_name']}_{r['run_id']}",
                        label_visibility="collapsed",
                    )

            # Show gold answer if available
            if gold:
                gold_text = df[df["question_id"] == qid]["gold_answer"].iloc[0]
                if gold_text:
                    st.markdown("**Réponse de référence (gold)**")
                    st.text_area(
                        "Gold",
                        value=str(gold_text)[:2000],
                        height=150,
                        disabled=True,
                        key=f"gold_{qid}",
                        label_visibility="collapsed",
                    )


# =============================================================================
# Footer
# =============================================================================
st.divider()
st.caption(f"Données: {len(df)} runs | {df['config_name'].nunique()} configs | {df['goldset_name'].nunique()} goldsets | {datetime.now().strftime('%H:%M:%S')}")
