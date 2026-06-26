"""Pipeline Timeline – reconstruct a single turn stage by stage from rag_trace_events.

This page is a compact ``chat_runs`` explorer. Selecting a run renders a vertical
timeline of its RAG pipeline (intent → retrieval/RRF → aggregation/rerank →
selection → context → generation), showing the chunks/sections that flowed through
each stage with their scores, durations and status. Runs predating the
``rag_trace_events`` table fall back to an approximate timeline rebuilt from the
``chat_runs.v3_*`` summary columns.
"""

import json
from datetime import timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import text

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.db_utils import get_engine

st.set_page_config(page_title="Pipeline Timeline", page_icon="🛰️", layout="wide")

require_admin()
show_admin_badge()

engine = get_engine()


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _as_obj(v):
    """JSONB → python object. Passthrough dict/list, parse JSON strings, else {}."""
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v != "":
        stripped = v.strip()
        if not stripped or stripped[0] not in "[{":
            return {}
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _fmt_time(ms) -> str:
    if ms is None or pd.isna(ms):
        return "—"
    ms = float(ms)
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


STAGE_META = {
    "query-processor": ("🧠", "Intent / Reformulation"),
    "retriever": ("🔍", "Retrieval + RRF"),
    "section-aggregator": ("🧩", "Agrégation + Rerank"),
    "context-selector": ("🎯", "Sélection LLM"),
    "context-builder": ("📄", "Construction du contexte"),
    "generator": ("✍️", "Génération"),
}

STATUS_ICON = {
    "ok": "🟢",
    "empty": "🟡",
    "disabled": "⚪",
    "all_rejected": "🔴",
    "skipped_no_sections": "🟡",
    "skipped_no_context": "🔴",
    "failed": "🔴",
    "short_circuit": "🟠",
}

TRACE_EVENT_REQUIRED_COLUMNS = {
    "event_index",
    "stage",
    "attempt_name",
    "duration_ms",
    "status",
    "input_ref",
    "output_ref",
    "metrics",
    "error_type",
    "error_message",
}


# ════════════════════════════════════════════════════════════════════════════
# Data loading
# ════════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=60)
def table_columns(table_name: str) -> set[str]:
    """Return public table columns, or an empty set when the table is absent."""
    if not engine:
        return set()
    sql = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name
    """)
    try:
        df = pd.read_sql_query(sql, engine, params={"table_name": table_name})
    except Exception:
        return set()
    return set(df["column_name"].tolist())


def _run_col(columns: set[str], name: str, default_sql: str) -> str:
    return f"r.{name}" if name in columns else f"{default_sql} AS {name}"


@st.cache_data(ttl=10)
def load_runs() -> pd.DataFrame:
    """Compact chat_runs list with latest feedback stars."""
    if not engine:
        return pd.DataFrame()
    run_columns = table_columns("chat_runs")
    if not {"ts", "turn_id", "question", "answer", "rag_version"}.issubset(run_columns):
        return pd.DataFrame()

    feedback_columns = table_columns("chat_feedbacks")
    with_clause = ""
    join_clause = ""
    stars_expr = "NULL::integer AS stars"
    if {"turn_id", "stars", "ts"}.issubset(feedback_columns):
        with_clause = """
        WITH last_fb AS (
          SELECT DISTINCT ON (turn_id) turn_id, stars
          FROM chat_feedbacks
          ORDER BY turn_id, ts DESC
        )
        """
        join_clause = "LEFT JOIN last_fb f USING (turn_id)"
        stars_expr = "f.stars"

    select_exprs = [
        "r.ts",
        _run_col(run_columns, "user_group", "NULL::text"),
        "r.question",
        "r.answer",
        _run_col(run_columns, "v3_intent", "NULL::text"),
        _run_col(run_columns, "v3_detected_theme", "NULL::text"),
        _run_col(run_columns, "v3_chunks_retrieved_count", "NULL::integer"),
        _run_col(run_columns, "v3_context_items_count", "NULL::integer"),
        _run_col(run_columns, "total_time_ms", "NULL::double precision"),
        "r.rag_version",
        "r.turn_id",
        _run_col(run_columns, "trace_id", "NULL::text"),
        stars_expr,
    ]
    sql = text(f"""
        {with_clause}
        SELECT
          {", ".join(select_exprs)}
        FROM chat_runs r
        {join_clause}
        WHERE r.rag_version = 'v3'
        ORDER BY r.ts DESC
        LIMIT 500
    """)
    try:
        df = pd.read_sql_query(sql, engine)
    except Exception:
        return pd.DataFrame()
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Paris")
    if "stars" in df.columns:
        df["stars"] = df["stars"].apply(lambda x: x + 1 if pd.notna(x) else x)
    return df


@st.cache_data(ttl=30)
def load_run_detail(turn_id: str) -> dict:
    """Full chat_runs row (for the v3_* fallback and TTFT) as a plain dict."""
    if not engine or not turn_id:
        return {}
    if "turn_id" not in table_columns("chat_runs"):
        return {}
    sql = text("SELECT * FROM chat_runs WHERE turn_id = :tid LIMIT 1")
    try:
        df = pd.read_sql_query(sql, engine, params={"tid": turn_id})
    except Exception:
        return {}
    if df.empty:
        return {}
    return df.iloc[0].to_dict()


@st.cache_data(ttl=30)
def load_trace_events(turn_id: str) -> pd.DataFrame:
    if not engine or not turn_id:
        return pd.DataFrame()
    trace_columns = table_columns("rag_trace_events")
    if not TRACE_EVENT_REQUIRED_COLUMNS.issubset(trace_columns):
        return pd.DataFrame()
    sql = text("""
        SELECT event_index, stage, attempt_name, duration_ms, status,
               input_ref, output_ref, metrics, error_type, error_message
        FROM rag_trace_events
        WHERE turn_id = :tid
        ORDER BY event_index ASC
    """)
    try:
        df = pd.read_sql_query(sql, engine, params={"tid": turn_id})
    except Exception:
        return pd.DataFrame()
    if "attempt_name" in df.columns:
        df["attempt_name"] = df["attempt_name"].fillna("")
    for col in ("input_ref", "output_ref", "metrics"):
        if col in df.columns:
            df[col] = df[col].apply(_as_obj)
    return df


# ════════════════════════════════════════════════════════════════════════════
# Runs table + selection
# ════════════════════════════════════════════════════════════════════════════
st.title("🛰️ Pipeline Timeline")

with st.sidebar:
    st.markdown("### ⚙️ Options")
    if st.button("🔄 Rafraîchir", width="stretch", type="primary"):
        table_columns.clear()
        load_runs.clear()
        load_trace_events.clear()
        load_run_detail.clear()
        st.rerun()
    st.caption("Données en cache 10-30s.")

if not engine:
    st.error("Pas de connexion à la base de données.")
    st.stop()

runs = load_runs()
if runs.empty:
    st.info("Aucun run V3 pour le moment.")
    st.stop()

q = st.text_input("🔎 Filtrer", placeholder="Rechercher question / réponse / intent...", label_visibility="collapsed")
period = st.pills("📅 Période", ["Tout", "Aujourd'hui", "Hier", "Cette semaine"], default="Tout", label_visibility="collapsed")

view = runs.copy()
if q:
    ql = q.lower()
    cols = ["question", "answer", "v3_intent", "v3_detected_theme", "user_group"]
    mask = view[cols].apply(lambda r: ql in " ".join(str(v) for v in r if pd.notna(v)).lower(), axis=1)
    view = view[mask]
if period and period != "Tout" and "ts" in view.columns:
    now = pd.Timestamp.now(tz="Europe/Paris")
    if period == "Aujourd'hui":
        view = view[view["ts"] >= now.normalize()]
    elif period == "Hier":
        y = (now - timedelta(days=1)).normalize()
        view = view[(view["ts"] >= y) & (view["ts"] < now.normalize())]
    elif period == "Cette semaine":
        view = view[view["ts"] >= (now - timedelta(days=now.weekday())).normalize()]

view = view.reset_index(drop=True)
st.caption(f"📊 {len(view)} run(s) — clic sur une ligne pour voir la timeline")

selection = st.dataframe(
    view,
    width="stretch",
    hide_index=True,
    height=380,
    on_select="rerun",
    selection_mode="single-row",
    key="runs_table",
    column_order=[
        "ts",
        "user_group",
        "question",
        "answer",
        "v3_intent",
        "v3_detected_theme",
        "v3_chunks_retrieved_count",
        "v3_context_items_count",
        "total_time_ms",
        "stars",
    ],
    column_config={
        "ts": st.column_config.DatetimeColumn("Date/heure", format="YYYY-MM-DD HH:mm"),
        "user_group": st.column_config.TextColumn("Groupe", width="small"),
        "question": st.column_config.TextColumn("Question", width="medium"),
        "answer": st.column_config.TextColumn("Réponse", width="large"),
        "v3_intent": st.column_config.TextColumn("Intent", width="small"),
        "v3_detected_theme": st.column_config.TextColumn("Thème", width="small"),
        "v3_chunks_retrieved_count": st.column_config.NumberColumn("Chunks", format="%d"),
        "v3_context_items_count": st.column_config.NumberColumn("Items ctx", format="%d"),
        "total_time_ms": st.column_config.NumberColumn("Total (ms)", format="%d"),
        "stars": st.column_config.NumberColumn("⭐", format="%d ⭐"),
    },
)

# Resolve selection → turn_id (persist across reruns; offer a selectbox fallback).
sel_rows = selection.selection["rows"] if selection and selection.selection else []
if sel_rows:
    st.session_state["timeline_turn_id"] = str(view.iloc[sel_rows[0]]["turn_id"])

turn_id = st.session_state.get("timeline_turn_id")
if turn_id and turn_id not in set(view["turn_id"]):
    turn_id = None  # filtered out

with st.expander("…ou sélectionner un run manuellement", expanded=not turn_id):
    opts = view["turn_id"].tolist()
    if opts:
        labels = {r["turn_id"]: f"{str(r['ts'])[:16]} — {str(r['question'])[:90]}" for _, r in view.iterrows()}
        picked = st.selectbox(
            "Run",
            options=opts,
            index=opts.index(turn_id) if turn_id in opts else 0,
            format_func=lambda x: labels.get(x, x),
            label_visibility="collapsed",
        )
        if picked:
            turn_id = picked
            st.session_state["timeline_turn_id"] = picked
    else:
        st.warning("Aucun run ne correspond aux filtres.")

if not turn_id:
    st.info("Sélectionne un run ci-dessus pour afficher sa timeline.")
    st.stop()

run_row = view[view["turn_id"] == turn_id].iloc[0]
detail = load_run_detail(turn_id)

st.divider()
st.subheader("🧵 Timeline du run")
st.markdown(f"**Question :** {run_row['question']}")
st.caption(f"turn_id `{turn_id}` · trace_id `{run_row.get('trace_id') or '—'}`")


# ════════════════════════════════════════════════════════════════════════════
# Per-stage render bodies
# ════════════════════════════════════════════════════════════════════════════
def _metrics_row(pairs):
    cols = st.columns(len(pairs))
    for col, (label, value) in zip(cols, pairs):
        col.metric(label, value)


def _body_query_processor(out, mtr, inp):
    _metrics_row(
        [
            ("Intent", str(out.get("intent") or "—")),
            ("Thème", str(out.get("theme") or "—")),
            ("Proceed", "✅" if out.get("should_proceed") else "⛔"),
            ("Réf. juridique", "✅" if out.get("needs_legal_search") else "—"),
        ]
    )
    if out.get("query_for_retrieval"):
        st.info(f"**Query retrieval :** {out['query_for_retrieval']}")
    acr = out.get("expanded_acronyms") or []
    if acr:
        st.caption("Acronymes : " + ", ".join(str(a) for a in acr))


def _body_retriever(out, mtr, inp):
    tables = inp.get("tables_searched") or []
    st.caption(
        f"Sources : {', '.join(str(table) for table in tables) or '—'}  ·  mode : {inp.get('search_mode', '—')}  ·  top_k : {inp.get('top_k', '—')}"
    )
    _metrics_row(
        [
            ("Chunks", mtr.get("chunk_count", 0)),
            ("Top score", mtr.get("top_score", 0.0)),
            ("Avg score", mtr.get("avg_score", 0.0)),
        ]
    )
    chunks = out.get("retrieved_chunks") or []
    if chunks:
        cdf = pd.DataFrame(chunks)
        keep = [c for c in ["table", "score", "retrieval_path", "heading", "section_id", "preview"] if c in cdf.columns]
        cdf = cdf[keep]
        if "table" in cdf.columns:
            sort_cols = ["table"] + (["score"] if "score" in cdf.columns else [])
            cdf = cdf.sort_values(sort_cols, ascending=[True] + ([False] if "score" in cdf.columns else []))
            for table_name, group in cdf.groupby("table", dropna=False):
                source_label = "source inconnue" if pd.isna(table_name) or not table_name else str(table_name)
                st.markdown(f"**{source_label}**")
                display_cols = [c for c in keep if c != "table"]
                st.dataframe(group[display_cols], hide_index=True, width="stretch")
        else:
            st.dataframe(cdf, hide_index=True, width="stretch")
        st.caption("Scores = score final post-fusion RRF, groupés par source. Les rankings pré-fusion par source ne sont pas tracés.")
    else:
        st.caption("Aucun chunk récupéré.")


def _body_section_aggregator(out, mtr, inp):
    _metrics_row(
        [
            ("Sections avant rerank", mtr.get("sections_before_rerank", 0)),
            ("Sections après rerank", mtr.get("sections_after_rerank", 0)),
            ("Reranker", str(mtr.get("reranker_status") or "—")),
        ]
    )
    sections = out.get("aggregated_sections") or []
    if sections:
        sdf = pd.DataFrame(sections)
        keep = [c for c in ["heading", "publisher", "score", "chunk_count", "tokens"] if c in sdf.columns]
        st.dataframe(sdf[keep], hide_index=True, width="stretch")
    else:
        st.caption("Aucune section.")


def _body_context_selector(out, mtr, inp):
    _metrics_row(
        [
            ("Items avant", mtr.get("items_before", 0)),
            ("Items après", mtr.get("items_after", 0)),
            ("Tout rejeté", "🔴 oui" if mtr.get("all_rejected") else "non"),
        ]
    )
    sections = out.get("selected_sections") or []
    if sections:
        sdf = pd.DataFrame(sections)
        keep = [c for c in ["heading", "publisher", "score", "tokens"] if c in sdf.columns]
        st.dataframe(sdf[keep], hide_index=True, width="stretch")
    if out.get("reason"):
        st.info(f"**Raisonnement :** {out['reason']}")
    if out.get("selector_decisions"):
        with st.expander("Décisions structurées"):
            st.json(out["selector_decisions"])


def _body_context_builder(out, mtr, inp):
    _metrics_row(
        [
            ("Items contexte", mtr.get("context_item_count", 0)),
            ("Tokens", mtr.get("context_tokens", 0)),
            ("Docs entiers", mtr.get("doc_entire_count", 0)),
        ]
    )
    items = out.get("context_items") or []
    if items:
        idf = pd.DataFrame(items)
        keep = [c for c in ["heading", "publisher", "score", "tokens", "is_doc_entire"] if c in idf.columns]
        st.dataframe(idf[keep], hide_index=True, width="stretch")
    else:
        st.caption("Aucun item de contexte.")


def _body_generator(out, mtr, inp):
    ttft = detail.get("v3_ttft_ms")
    _metrics_row(
        [
            ("Tokens réponse", mtr.get("answer_tokens", 0)),
            ("Sources", out.get("sources_count", 0)),
            ("TTFT", _fmt_time(ttft)),
        ]
    )
    if out.get("answer_preview"):
        st.info(f"**Réponse (aperçu) :** {out['answer_preview']}")


_STAGE_BODY = {
    "query-processor": _body_query_processor,
    "retriever": _body_retriever,
    "section-aggregator": _body_section_aggregator,
    "context-selector": _body_context_selector,
    "context-builder": _body_context_builder,
    "generator": _body_generator,
}


def render_event(row):
    stage = row["stage"]
    icon, label = STAGE_META.get(stage, ("•", stage))
    rail, body = st.columns([2, 5])
    with rail:
        st.markdown(f"**{icon} {label}**")
        if row.get("attempt_name"):
            st.caption(f"attempt : {row['attempt_name']}")
        st.caption(f"{STATUS_ICON.get(row['status'], '•')} {row['status']}  |  ⏱ {_fmt_time(row['duration_ms'])}")
    with body:
        renderer = _STAGE_BODY.get(stage)
        if renderer:
            renderer(row.get("output_ref") or {}, row.get("metrics") or {}, row.get("input_ref") or {})
        else:
            st.json(row.get("output_ref") or {})
        if row.get("error_message"):
            st.error(f"{row.get('error_type') or 'error'} : {row['error_message']}")


# ════════════════════════════════════════════════════════════════════════════
# Fallback timeline (no trace events) — rebuilt from chat_runs.v3_* columns
# ════════════════════════════════════════════════════════════════════════════
def render_v3_fallback_timeline(d: dict):
    steps = [
        ("🧠", "Intent / Reformulation", "v3_query_processing_ms", [("Intent", d.get("v3_intent")), ("Thème", d.get("v3_detected_theme"))]),
        ("🔍", "Retrieval + RRF", "v3_retrieval_ms", [("Chunks", d.get("v3_chunks_retrieved_count"))]),
        (
            "🧩",
            "Agrégation + Rerank",
            "v3_aggregation_ms",
            [("Avant", d.get("v3_sections_before_rerank")), ("Après", d.get("v3_sections_after_rerank"))],
        ),
        ("🎯", "Sélection LLM", "v3_selector_ms", [("Sélectionnées", d.get("v3_selector_selected_count"))]),
        (
            "📄",
            "Construction du contexte",
            "v3_context_building_ms",
            [("Items", d.get("v3_context_items_count")), ("Tokens", d.get("v3_context_tokens"))],
        ),
        ("✍️", "Génération", "v3_generation_ms", [("TTFT", _fmt_time(d.get("v3_ttft_ms")))]),
    ]
    for icon, label, time_key, facts in steps:
        rail, body = st.columns([2, 5])
        with rail:
            st.markdown(f"**{icon} {label}**")
            st.caption(f"⏱ {_fmt_time(d.get(time_key))}")
        with body:
            _metrics_row([(lbl, "—" if v is None or (isinstance(v, float) and pd.isna(v)) else v) for lbl, v in facts])


# ════════════════════════════════════════════════════════════════════════════
# Render: timeline or fallback
# ════════════════════════════════════════════════════════════════════════════
trace_df = load_trace_events(turn_id)

if trace_df.empty:
    trace_columns = table_columns("rag_trace_events")
    if not trace_columns:
        message = "Table rag_trace_events absente. Reconstruction approximative depuis chat_runs ci-dessous."
    elif not TRACE_EVENT_REQUIRED_COLUMNS.issubset(trace_columns):
        missing = sorted(TRACE_EVENT_REQUIRED_COLUMNS - trace_columns)
        message = f"Schéma rag_trace_events incomplet ({', '.join(missing)} manquant). Reconstruction approximative depuis chat_runs ci-dessous."
    else:
        message = (
            "Aucun événement de trace pour ce run (run antérieur à rag_trace_events "
            "ou tracing désactivé). Reconstruction approximative depuis chat_runs ci-dessous."
        )
    st.info(message)
    render_v3_fallback_timeline(detail)
    st.stop()

# Attempt selector (a selector_retry re-runs retrieval→…→builder).
attempts = [a for a in trace_df["attempt_name"].dropna().unique().tolist() if a]
final_attempt = attempts[-1] if attempts else None
chosen_attempt = None
if len(attempts) > 1:
    chosen_attempt = st.segmented_control(
        "Tentative",
        attempts,
        default=attempts[-1],
        help="Une nouvelle tentative (selector_retry) relance le retrieval après un rejet du Selector.",
    )

events = trace_df.copy()
# Query processing is global. Generation is also untagged, but belongs to the
# final selected context, so only show it on the final attempt view.
if chosen_attempt:
    untagged = events["attempt_name"].fillna("").eq("")
    global_stages = ["query-processor"]
    if chosen_attempt == final_attempt:
        global_stages.append("generator")
    global_mask = untagged & events["stage"].isin(global_stages)
    events = events[(events["attempt_name"] == chosen_attempt) | global_mask]
# Defensive de-dup of query-processor (keep lowest event_index).
qp = events[events["stage"] == "query-processor"]
if len(qp) > 1:
    drop_idx = qp.sort_values("event_index").iloc[1:].index
    events = events.drop(index=drop_idx)
events = events.sort_values("event_index").reset_index(drop=True)

if chosen_attempt and final_attempt and chosen_attempt != final_attempt:
    st.info(f"La génération finale est rattachée à `{final_attempt}`. Sélectionne cette tentative pour afficher la réponse générée.")


# Flow summary cascade.
def _stage_metrics(stage):
    rows = events[events["stage"] == stage]
    return rows.iloc[-1]["metrics"] if len(rows) else {}


r_m, a_m = _stage_metrics("retriever"), _stage_metrics("section-aggregator")
s_m, c_m = _stage_metrics("context-selector"), _stage_metrics("context-builder")
flow = st.columns(6)
flow[0].metric("Chunks récupérés", r_m.get("chunk_count", 0))
flow[1].metric("Sections (avant rerank)", a_m.get("sections_before_rerank", 0))
flow[2].metric("Sections (après rerank)", a_m.get("sections_after_rerank", 0))
flow[3].metric("Sélectionnées", s_m.get("items_after", 0))
flow[4].metric("Items contexte", c_m.get("context_item_count", 0))
flow[5].metric("Latence totale", _fmt_time(detail.get("total_time_ms") or run_row.get("total_time_ms")))

st.markdown("")  # spacer

for i, row in events.iterrows():
    render_event(row)
    if i < len(events) - 1:
        st.divider()
