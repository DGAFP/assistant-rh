"""
Golden Beta Analysis - Analyse question par question des résultats des judges.

Compare la config optimale (v3_optim) avec les configs beta (v2_prod, v3_prod).
Affiche les résultats du Judge 1 (catégorisation d'erreur) et Judge 2 (comparaison beta).
"""

import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from assistant_rh_rag_pipeline.db_helpers import get_dsn as get_app_dsn
from dotenv import load_dotenv

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.private_datasets import PrivateDatasetError, resolve_golden_beta_files

load_dotenv()

require_admin()
show_admin_badge()

# ---------- Page config ----------
st.set_page_config(page_title="Golden Beta Analysis", page_icon="🏅", layout="wide")

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------- DB Connection ----------

def get_db_connection():
    """Get a fresh DB connection using psycopg (dict rows)."""
    import psycopg
    from psycopg.rows import dict_row

    try:
        dsn = os.getenv("TUNNEL_DSN") or get_app_dsn()
    except RuntimeError:
        return None
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute("SELECT 1")
        return conn
    except psycopg.Error as e:
        st.warning(f"DB non disponible: {e}. Les données CSV sont affichées.")
        return None


# ---------- Data Loading ----------

@st.cache_data(ttl=600, show_spinner="Chargement des fichiers Golden Beta...")
def load_judge_csvs() -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Load judge results from local CSVs or return None after showing an error."""
    try:
        files = resolve_golden_beta_files(local_dir=REPO_ROOT / "data" / "golden_beta")
    except PrivateDatasetError as exc:
        st.error(str(exc))
        st.info(
            "Configurez `HF_TOKEN` et `ASSISTANT_RH_PRIVATE_DATASET_REPO` "
            "si les CSV Golden Beta ne sont pas présents localement."
        )
        return None

    st.caption(f"Source Golden Beta : `{files.source}`")
    return pd.read_csv(files.judge1_path), pd.read_csv(files.judge2_path)


@st.cache_data(ttl=120, show_spinner="Chargement des donnees DB...")
def load_db_data():
    """Load RAGAS metrics, responses, and feedback from DB (cached 2 min)."""
    conn = get_db_connection()
    if conn is None:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, question, theme, goldset_name, gold_answer, gold_sources, tags
                FROM goldset_questions_v2
                WHERE tags @> ARRAY['golden_beta']
                ORDER BY id
            """)
            questions = {r['id']: r for r in cur.fetchall()}

            qids = list(questions.keys())
            if not qids:
                return None

            placeholders = ','.join(['%s'] * len(qids))
            cur.execute(f"""
                SELECT
                    gr.id as run_id,
                    gr.question_id,
                    gr.config_name,
                    gr.response,
                    gr.metrics,
                    gr.retrieval_time_ms,
                    gr.generation_time_ms,
                    gr.retrieved_context
                FROM goldset_runs gr
                WHERE gr.question_id IN ({placeholders})
                  AND gr.config_name IN ('v3_optim', 'v2_prod', 'v3_prod')
                ORDER BY gr.question_id, gr.config_name
            """, qids)
            runs = cur.fetchall()

            cur.execute(f"""
                SELECT
                    gq.id as question_id,
                    cf.stars,
                    cf.helpful,
                    cf.comment,
                    cf.reasons_positive,
                    cf.reasons_negative,
                    cf.error_category
                FROM goldset_questions_v2 gq
                LEFT JOIN chat_feedbacks cf ON gq.original_turn_id = cf.turn_id
                WHERE gq.id IN ({placeholders})
            """, qids)
            feedbacks = {r['question_id']: r for r in cur.fetchall()}

        db_data = {}
        for r in runs:
            qid = r['question_id']
            if qid not in db_data:
                db_data[qid] = {
                    'question_meta': questions.get(qid, {}),
                    'feedback': feedbacks.get(qid, {}),
                    'runs': {},
                }
            db_data[qid]['runs'][r['config_name']] = r

        return db_data

    except Exception as e:
        st.warning(f"Erreur chargement DB: {e}")
        return None


def extract_metrics(metrics_json):
    """Safely extract metrics from JSONB."""
    if not metrics_json:
        return {}
    if isinstance(metrics_json, str):
        try:
            return json.loads(metrics_json)
        except Exception:
            return {}
    return metrics_json


# ---------- UI ----------

st.title("🏅 Golden Beta Analysis")
st.markdown("Analyse des résultats du pipeline optimal vs beta-test, avec jugements LLM.")
st.info(
    "📋 **Analyse de fin de mission (fév. 2026)** — Cette page présente l'évaluation "
    "comparative du pipeline RAG réalisée pendant le beta-test (08/01 – 06/02/2026). "
    "Elle peut être archivée une fois que l'équipe en a pris connaissance.",
    icon="ℹ️",
)

col_title, col_refresh = st.columns([8, 1])
with col_refresh:
    if st.button("🔄", help="Rafraichir les donnees DB"):
        load_db_data.clear()
        st.rerun()

result = load_judge_csvs()
if result is None:
    st.stop()
df_j1, df_j2 = result

db_data = load_db_data()

if not db_data:
    st.warning("⚠️ Donnees DB non disponibles (RAGAS, themes, reponses, feedback). "
               "Verifiez le tunnel DB et cliquez 🔄 pour rafraichir.")

# Merge judge results
df_merged = df_j1[['question_id', 'category', 'confidence', 'explanation',
                     'is_satisfactory', 'severity', 'improvement_suggestion', 'question']].copy()
df_merged.columns = ['question_id', 'j1_category', 'j1_confidence', 'j1_explanation',
                      'j1_satisfactory', 'j1_severity', 'j1_suggestion', 'question']

j2_cols = ['question_id', 'quality_comparison', 'score_beta', 'score_optimal',
           'explanation', 'key_improvements', 'regressions']
df_j2_subset = df_j2[[c for c in j2_cols if c in df_j2.columns]].copy()
df_j2_subset.columns = ['question_id'] + [f'j2_{c}' for c in df_j2_subset.columns[1:]]

df = df_merged.merge(df_j2_subset, on='question_id', how='left')

# Add theme and feedback from DB
if db_data:
    df['theme'] = df['question_id'].map(
        lambda qid: db_data.get(qid, {}).get('question_meta', {}).get('theme', ''))
    def _get_stars(qid):
        s = db_data.get(qid, {}).get('feedback', {}).get('stars')
        return s + 1 if s is not None else None
    df['feedback_stars'] = df['question_id'].map(_get_stars)
    df['feedback_error'] = df['question_id'].map(
        lambda qid: db_data.get(qid, {}).get('feedback', {}).get('error_category', ''))

    # Add RAGAS metrics for v3_optim
    def get_metric(qid, metric_name):
        runs = db_data.get(qid, {}).get('runs', {})
        optim = runs.get('v3_optim')
        if optim:
            m = extract_metrics(optim.get('metrics'))
            return m.get(metric_name)
        return None

    df['faithfulness'] = df['question_id'].map(lambda qid: get_metric(qid, 'faithfulness_41mini'))
    df['answer_relevancy'] = df['question_id'].map(lambda qid: get_metric(qid, 'answer_relevancy_41mini'))

# ---------- Tabs ----------
tab_overview, tab_detail, tab_regressions = st.tabs([
    "Vue d'ensemble", "Detail par question", "Regressions & Axes d'amelioration"
])

# ==================== TAB 1: OVERVIEW ====================
with tab_overview:
    st.header("Synthese")

    col1, col2, col3, col4 = st.columns(4)
    n_total = len(df)
    n_satisfactory = df['j1_satisfactory'].sum() if 'j1_satisfactory' in df.columns else 0

    with col1:
        st.metric("Questions evaluees", n_total)
    with col2:
        st.metric("Satisfaisantes (J1)", f"{n_satisfactory}/{n_total}",
                   delta=f"{n_satisfactory/n_total*100:.0f}%" if n_total else "0%")
    with col3:
        n_better = (df['j2_quality_comparison'] == 'better').sum() if 'j2_quality_comparison' in df.columns else 0
        st.metric("Meilleures que beta (J2)", f"{n_better}/{n_total}",
                   delta=f"{n_better/n_total*100:.0f}%")
    with col4:
        n_worse = (df['j2_quality_comparison'] == 'worse').sum() if 'j2_quality_comparison' in df.columns else 0
        st.metric("Regressions", f"{n_worse}",
                   delta=f"-{n_worse/n_total*100:.1f}%" if n_total else "0%",
                   delta_color="inverse")

    st.divider()

    # Charts side by side
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Judge 1 - Categories d'erreur")
        cat_counts = df['j1_category'].value_counts().reset_index()
        cat_counts.columns = ['category', 'count']

        color_map = {
            'correct': '#2ecc71',
            'generator_hallucination': '#e74c3c',
            'context_insufficient': '#f39c12',
            'generator_incomplete': '#e67e22',
            'retrieval_miss': '#9b59b6',
            'generator_misinterpretation': '#3498db',
            'selector_error': '#1abc9c',
            'question_ambiguous': '#95a5a6',
            'error': '#7f8c8d',
        }

        fig1 = px.pie(cat_counts, values='count', names='category',
                       color='category', color_discrete_map=color_map,
                       hole=0.4)
        fig1.update_layout(height=350, margin=dict(t=20, b=20))
        st.plotly_chart(fig1, width="stretch", key="j1_pie")

    with col_right:
        st.subheader("Judge 2 - Comparaison vs Beta")
        if 'j2_quality_comparison' in df.columns:
            comp_counts = df['j2_quality_comparison'].value_counts().reset_index()
            comp_counts.columns = ['comparison', 'count']
            comp_color = {'better': '#2ecc71', 'equal': '#f39c12', 'worse': '#e74c3c'}
            fig2 = px.bar(comp_counts, x='comparison', y='count', color='comparison',
                          color_discrete_map=comp_color)
            fig2.update_layout(height=350, margin=dict(t=20, b=20), showlegend=False)
            st.plotly_chart(fig2, width="stretch", key="j2_bar")

    # Cross analysis
    st.subheader("Analyse croisee : Categorie d'erreur x Comparaison beta")
    if 'j2_quality_comparison' in df.columns:
        cross = pd.crosstab(df['j1_category'], df['j2_quality_comparison'])
        for col in ['better', 'equal', 'worse']:
            if col not in cross.columns:
                cross[col] = 0
        cross = cross[['better', 'equal', 'worse']].sort_values('worse', ascending=False)
        st.dataframe(cross, width="stretch")

    # RAGAS metrics
    if 'faithfulness' in df.columns and df['faithfulness'].notna().any():
        st.subheader("Metriques RAGAS (v3_optim)")
        col_f, col_r = st.columns(2)
        with col_f:
            avg_f = df['faithfulness'].mean()
            st.metric("Faithfulness moyen", f"{avg_f:.2f}" if pd.notna(avg_f) else "N/A")
        with col_r:
            avg_r = df['answer_relevancy'].mean() if 'answer_relevancy' in df.columns else None
            st.metric("Answer Relevancy moyen", f"{avg_r:.2f}" if pd.notna(avg_r) else "N/A")

    # By theme
    if 'theme' in df.columns and df['theme'].notna().any():
        st.subheader("Par theme")
        theme_stats = df.groupby('theme').agg(
            total=('question_id', 'count'),
            satisfactory=('j1_satisfactory', 'sum'),
            better=('j2_quality_comparison', lambda x: (x == 'better').sum()),
            worse=('j2_quality_comparison', lambda x: (x == 'worse').sum()),
        ).reset_index()
        theme_stats['pct_satisfactory'] = (theme_stats['satisfactory'] / theme_stats['total'] * 100).round(1)
        theme_stats['pct_better'] = (theme_stats['better'] / theme_stats['total'] * 100).round(1)
        theme_stats = theme_stats.sort_values('total', ascending=False)
        st.dataframe(theme_stats, width="stretch", hide_index=True)


# ==================== TAB 2: DETAIL ====================
with tab_detail:
    st.header("Detail par question")

    # Filters
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)

    with col_f1:
        cat_options = ['Tous'] + sorted(df['j1_category'].unique().tolist())
        selected_cat = st.selectbox("Categorie J1", cat_options)

    with col_f2:
        comp_options = ['Tous', 'better', 'equal', 'worse']
        selected_comp = st.selectbox("Comparaison J2", comp_options)

    with col_f3:
        theme_options = ['Tous']
        if 'theme' in df.columns:
            theme_options += sorted([t for t in df['theme'].unique() if pd.notna(t) and t])
        selected_theme = st.selectbox("Theme", theme_options)

    with col_f4:
        severity_options = ['Tous'] + sorted(
            [s for s in df['j1_severity'].unique() if pd.notna(s)])
        selected_severity = st.selectbox("Severite", severity_options)

    # Apply filters
    df_filtered = df.copy()
    if selected_cat != 'Tous':
        df_filtered = df_filtered[df_filtered['j1_category'] == selected_cat]
    if selected_comp != 'Tous':
        df_filtered = df_filtered[df_filtered['j2_quality_comparison'] == selected_comp]
    if selected_theme != 'Tous':
        df_filtered = df_filtered[df_filtered['theme'] == selected_theme]
    if selected_severity != 'Tous':
        df_filtered = df_filtered[df_filtered['j1_severity'] == selected_severity]

    st.caption(f"{len(df_filtered)} questions affichees / {len(df)} total")

    # Summary table
    display_cols = ['question_id', 'question', 'j1_category', 'j1_satisfactory',
                    'j1_severity', 'j2_quality_comparison']
    for col in ['j2_score_optimal', 'j2_score_beta', 'faithfulness',
                'answer_relevancy', 'theme', 'feedback_stars']:
        if col in df_filtered.columns:
            display_cols.append(col)

    existing_cols = [c for c in display_cols if c in df_filtered.columns]
    st.dataframe(
        df_filtered[existing_cols].sort_values('question_id'),
        width="stretch",
        hide_index=True,
        height=400,
    )

    st.divider()

    # Detailed question viewer
    st.subheader("Explorer une question")
    question_options = df_filtered.sort_values('question_id')['question_id'].tolist()
    if question_options:
        selected_qid = st.selectbox(
            "Question",
            question_options,
            format_func=lambda qid: f"Q{qid}: {df[df['question_id']==qid]['question'].iloc[0][:80]}..."
        )

        row = df[df['question_id'] == selected_qid].iloc[0]

        # Judge results side by side
        col_j1, col_j2 = st.columns(2)

        with col_j1:
            st.markdown("### Judge 1 - Categorisation")
            cat = row['j1_category']
            cat_color = {
                'correct': '🟢', 'generator_hallucination': '🔴',
                'context_insufficient': '🟠', 'generator_incomplete': '🟡',
                'retrieval_miss': '🟣', 'generator_misinterpretation': '🔵',
                'selector_error': '🔵', 'question_ambiguous': '⚪',
            }.get(cat, '⚫')
            st.markdown(f"**Categorie**: {cat_color} `{cat}`")
            st.markdown(f"**Satisfaisant**: {'✅' if row.get('j1_satisfactory') else '❌'}")
            st.markdown(f"**Severite**: `{row.get('j1_severity', 'N/A')}`")
            st.markdown(f"**Confiance**: {row.get('j1_confidence', 'N/A')}")
            st.markdown(f"**Explication**: {row.get('j1_explanation', 'N/A')}")
            if pd.notna(row.get('j1_suggestion')) and row['j1_suggestion']:
                st.markdown(f"**Suggestion**: {row['j1_suggestion']}")

        with col_j2:
            st.markdown("### Judge 2 - Comparaison beta")
            comp = row.get('j2_quality_comparison', 'N/A')
            comp_icon = {'better': '🟢 Meilleure', 'equal': '🟡 Egale', 'worse': '🔴 Pire'}.get(comp, comp)
            st.markdown(f"**Qualite**: {comp_icon}")
            st.markdown(f"**Score beta**: {row.get('j2_score_beta', 'N/A')}/5")
            st.markdown(f"**Score optimal**: {row.get('j2_score_optimal', 'N/A')}/5")
            st.markdown(f"**Explication**: {row.get('j2_explanation', 'N/A')}")
            if pd.notna(row.get('j2_key_improvements')):
                improvements = row['j2_key_improvements']
                if isinstance(improvements, str):
                    try:
                        improvements = json.loads(improvements.replace("'", '"'))
                    except Exception:
                        improvements = [improvements]
                if improvements:
                    st.markdown("**Ameliorations cles**:")
                    for imp in improvements:
                        st.markdown(f"- {imp}")

        # RAGAS metrics
        if 'faithfulness' in df.columns:
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                f_val = row.get('faithfulness')
                st.metric("Faithfulness", f"{f_val:.2f}" if pd.notna(f_val) else "N/A")
            with col_m2:
                r_val = row.get('answer_relevancy')
                st.metric("Answer Relevancy", f"{r_val:.2f}" if pd.notna(r_val) else "N/A")

        # Feedback utilisateur
        if db_data and selected_qid in db_data:
            fb = db_data[selected_qid].get('feedback', {})
            if fb and fb.get('stars'):
                st.divider()
                st.markdown("### Feedback utilisateur (beta)")
                col_fb1, col_fb2 = st.columns(2)
                with col_fb1:
                    stars_raw = fb.get('stars')
                    stars = stars_raw + 1 if stars_raw is not None else None
                    st.markdown(f"**Note**: {'⭐' * int(stars)} ({stars}/5)" if stars else "N/A")
                    if fb.get('error_category'):
                        st.markdown(f"**Categorie erreur**: `{fb['error_category']}`")
                with col_fb2:
                    if fb.get('comment'):
                        st.markdown(f"**Commentaire**: {fb['comment']}")

        # Responses comparison
        if db_data and selected_qid in db_data:
            st.divider()
            runs = db_data[selected_qid].get('runs', {})

            optim_run = runs.get('v3_optim')
            beta_run = runs.get('v3_prod') or runs.get('v2_prod')

            col_resp1, col_resp2 = st.columns(2)

            with col_resp1:
                st.markdown("### Reponse optimale (v3_optim)")
                if optim_run:
                    st.markdown(optim_run.get('response', 'N/A'))
                else:
                    st.warning("Pas de run v3_optim")

            with col_resp2:
                beta_cfg = 'v3_prod' if 'v3_prod' in runs else 'v2_prod'
                st.markdown(f"### Reponse beta ({beta_cfg})")
                if beta_run:
                    st.markdown(beta_run.get('response', 'N/A'))
                else:
                    st.warning("Pas de run beta")

            # Context viewer (collapsible)
            if optim_run and optim_run.get('retrieved_context'):
                with st.expander("Contexte retrieve (v3_optim)", expanded=False):
                    ctx = optim_run['retrieved_context']
                    if isinstance(ctx, str):
                        try:
                            ctx = json.loads(ctx)
                        except Exception:
                            ctx = [{'text': ctx}]
                    if isinstance(ctx, list):
                        for idx, item in enumerate(ctx):
                            if isinstance(item, dict):
                                source = item.get('source_name') or item.get('publisher') or ''
                                score = item.get('score', '')
                                text = item.get('text', '')
                                st.markdown(f"**[{idx+1}]** `{source}` | score={score}")
                                st.text(text[:500] + ('...' if len(text) > 500 else ''))
                                st.divider()
                    else:
                        st.json(ctx)


# ==================== TAB 3: REGRESSIONS ====================
with tab_regressions:
    st.header("Regressions & Axes d'amelioration")

    # Regressions
    st.subheader("🔴 Questions ou v3_optim est pire que le beta")
    df_worse = df[df['j2_quality_comparison'] == 'worse'].copy()
    if len(df_worse) > 0:
        for _, row in df_worse.iterrows():
            with st.expander(f"Q{row['question_id']}: {row['question'][:100]}...", expanded=False):
                st.markdown(f"**Categorie**: `{row['j1_category']}`")
                st.markdown(f"**Score beta**: {row.get('j2_score_beta', '?')}/5 | **Score optimal**: {row.get('j2_score_optimal', '?')}/5")
                st.markdown(f"**Explication J2**: {row.get('j2_explanation', '')}")
                st.markdown(f"**Explication J1**: {row.get('j1_explanation', '')}")
                if pd.notna(row.get('j2_regressions')):
                    regressions = row['j2_regressions']
                    if isinstance(regressions, str):
                        try:
                            regressions = json.loads(regressions.replace("'", '"'))
                        except Exception:
                            regressions = [regressions]
                    if regressions:
                        st.markdown("**Regressions identifiees**:")
                        for reg in regressions:
                            st.markdown(f"- {reg}")

                if db_data and row['question_id'] in db_data:
                    runs = db_data[row['question_id']].get('runs', {})
                    optim = runs.get('v3_optim')
                    beta = runs.get('v3_prod') or runs.get('v2_prod')
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("**Reponse optimale**:")
                        st.markdown(optim.get('response', 'N/A')[:800] if optim else 'N/A')
                    with c2:
                        st.markdown("**Reponse beta**:")
                        st.markdown(beta.get('response', 'N/A')[:800] if beta else 'N/A')
    else:
        st.success("Aucune regression !")

    st.divider()

    # Retrieval misses
    st.subheader("🟣 Retrieval miss - documents non trouves")
    df_ret_miss = df[df['j1_category'] == 'retrieval_miss']
    if len(df_ret_miss) > 0:
        for _, row in df_ret_miss.iterrows():
            st.markdown(f"- **Q{row['question_id']}**: {row['question'][:120]}")
            st.markdown(f"  *{row.get('j1_explanation', '')}*")
    else:
        st.success("Aucun retrieval miss !")

    # Context insufficient
    st.subheader("🟠 Contexte insuffisant")
    df_ctx = df[df['j1_category'] == 'context_insufficient']
    if len(df_ctx) > 0:
        for _, row in df_ctx.iterrows():
            comp_icon = {'better': '🟢', 'equal': '🟡', 'worse': '🔴'}.get(
                row.get('j2_quality_comparison', ''), '⚫')
            st.markdown(
                f"- {comp_icon} **Q{row['question_id']}**: {row['question'][:120]}")
            st.markdown(f"  *{row.get('j1_explanation', '')}*")
    else:
        st.success("Aucun contexte insuffisant !")

    # Hallucinations that are worse
    st.subheader("🔴 Hallucinations degradantes")
    df_hallu_worse = df[
        (df['j1_category'] == 'generator_hallucination') &
        (df['j2_quality_comparison'] == 'worse')
    ]
    if len(df_hallu_worse) > 0:
        for _, row in df_hallu_worse.iterrows():
            st.markdown(f"- **Q{row['question_id']}**: {row['question'][:120]}")
            st.markdown(f"  *J1: {row.get('j1_explanation', '')}*")
            st.markdown(f"  *J2: {row.get('j2_explanation', '')}*")
    else:
        st.success("Aucune hallucination degradante !")

    # Summary stats for improvement priorities
    st.divider()
    st.subheader("Priorites d'amelioration")

    priorities = []

    n_ret_miss = len(df[df['j1_category'] == 'retrieval_miss'])
    if n_ret_miss > 0:
        priorities.append(('🟣 Retrieval miss', n_ret_miss, 'Critique',
                           'Enrichir les sources, ajouter des documents manquants'))

    n_ctx = len(df[df['j1_category'] == 'context_insufficient'])
    if n_ctx > 0:
        priorities.append(('🟠 Contexte insuffisant', n_ctx, 'Haute',
                           'Ameliorer le retrieval ou elargir le context budget'))

    n_hallu = len(df[df['j1_category'] == 'generator_hallucination'])
    n_hallu_worse = len(df_hallu_worse)
    if n_hallu_worse > 0:
        priorities.append(('🔴 Hallucinations degradantes', n_hallu_worse, 'Haute',
                           'Renforcer le grounding dans le prompt systeme'))

    n_selector = len(df[df['j1_category'] == 'selector_error'])
    if n_selector > 0:
        priorities.append(('🔵 Selector error', n_selector, 'Moyenne',
                           'Ajuster le prompt du LLM Selector'))

    n_incomplete = len(df[df['j1_category'] == 'generator_incomplete'])
    if n_incomplete > 0:
        priorities.append(('🟡 Reponse incomplete', n_incomplete, 'Moyenne',
                           'Ajuster le prompt de generation pour plus de completude'))

    if priorities:
        df_priorities = pd.DataFrame(priorities,
                                      columns=['Probleme', 'Count', 'Priorite', 'Action suggeree'])
        st.dataframe(df_priorities, width="stretch", hide_index=True)
    else:
        st.success("Aucune priorite d'amelioration identifiee !")

    # Final note
    st.info(
        f"**Note**: {n_hallu - n_hallu_worse} hallucinations sur {n_hallu} "
        f"produisent des reponses **meilleures** que le beta. "
        f"Le generateur ajoute des connaissances utiles non presentes dans le contexte."
    )
