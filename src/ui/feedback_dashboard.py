"""
Feedback Dashboard - helpers purs (filtres, libellés et export Excel).

Extrait de 03_Feedback_Dashboard.py pour être testable sans Streamlit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
from assistant_rh_rag_pipeline.ministry_scope import resolve_ministry

BETA_START = date(2026, 1, 8)
BETA_END = date(2026, 2, 6)

PERIOD_ALL = "all"
PERIOD_LAST_MONTH = "last_month"
PERIOD_BETA = "beta"
PERIOD_CUSTOM = "custom"

PERIOD_MODE_OPTIONS = (PERIOD_ALL, PERIOD_LAST_MONTH, PERIOD_BETA, PERIOD_CUSTOM)

PERIOD_MODE_LABELS = {
    PERIOD_ALL: "📅 Tout",
    PERIOD_LAST_MONTH: "📆 Mois dernier",
    PERIOD_BETA: "🧪 Beta-test (8 jan – 6 fév 2026)",
    PERIOD_CUSTOM: "🗓️ Personnalisée",
}

MINISTRY_NOT_SET_LABEL = "Non renseigné"
PARIS_TIMEZONE = ZoneInfo("Europe/Paris")

QUESTION_EXPORT_COLUMNS = (
    ("ts", "Date"),
    ("user_group", "Groupe utilisateur"),
    ("selected_ministry", "Ministère"),
    ("question", "Question"),
    ("answer", "Réponse"),
    ("theme", "Thème"),
    ("turn_id", "turn_id"),
    ("session_id", "session_id"),
    ("rag_version", "rag_version"),
    ("chunk_selection_mode", "chunk_selection_mode"),
    ("dist_after_rerank", "dist_after_rerank"),
    ("total_time_ms", "total_time_ms"),
)

FEEDBACK_EXPORT_COLUMNS = (
    ("feedback_id", "feedback_id"),
    ("feedback_ts", "Date du feedback"),
    ("feedback_stars", "Note (1-5)"),
    ("feedback_helpful", "Utile"),
    ("feedback_reasons_positive", "Raisons positives"),
    ("feedback_reasons_negative", "Raisons négatives"),
    ("feedback_comment", "Commentaire"),
    ("feedback_beta_scope", "Périmètre bêta-test"),
    ("feedback_error_category", "Catégorie d’erreur"),
    ("feedback_ai_reason", "Raison de l’analyse automatique"),
)


def _complete_range(candidate: tuple | None) -> tuple[date, date] | None:
    """Return (start, end) when *candidate* holds two dates, else ``None``."""
    if candidate is not None and len(candidate) == 2 and candidate[0] and candidate[1]:
        return (candidate[0], candidate[1])
    return None


def current_paris_date(reference_datetime: datetime | None = None) -> date:
    """Return the calendar date in Paris, independently of server timezone."""
    instant = reference_datetime or datetime.now(PARIS_TIMEZONE)
    return instant.astimezone(PARIS_TIMEZONE).date()


def previous_calendar_month(reference_date: date | None = None) -> tuple[date, date]:
    """Return the first and last day of the previous calendar month."""
    first_of_current_month = (reference_date or current_paris_date()).replace(day=1)
    last_of_previous_month = first_of_current_month - timedelta(days=1)
    return (last_of_previous_month.replace(day=1), last_of_previous_month)


def resolve_period(
    mode: str,
    custom_range: tuple | None = None,
    last_applied: tuple | None = None,
    *,
    reference_date: date | None = None,
) -> tuple[date, date] | None:
    """Return the (start, end) bounds to apply, or ``None`` for no bound.

    Mode « Tout » returns ``None``: no frozen end date, so data added after the
    view was first opened stays visible on every rerun. « Mois dernier » is the
    previous calendar month and the beta-test preset is a fixed window. A
    custom range applies when complete (2 dates —
    ``st.date_input`` returns a 1-tuple mid-selection); while incomplete, the
    last complete custom range (*last_applied*) keeps applying so the view
    never silently widens to « Tout ».
    """
    if mode == PERIOD_BETA:
        return (BETA_START, BETA_END)
    if mode == PERIOD_LAST_MONTH:
        return previous_calendar_month(reference_date)
    if mode == PERIOD_CUSTOM:
        return _complete_range(custom_range) or _complete_range(last_applied)
    return None


def period_caption(mode: str, period: tuple[date, date] | None, data_min: date | None = None, data_max: date | None = None) -> str:
    """Human-readable description of the effectively applied period."""

    def _fmt(d: date) -> str:
        return d.strftime("%d/%m/%Y")

    if mode == PERIOD_CUSTOM and period is None:
        return "Personnalisée — sélection en cours, aucune borne appliquée"
    if period is None:
        if data_min is not None and data_max is not None:
            return f"Tout — du {_fmt(data_min)} au {_fmt(data_max)} (suit les nouvelles données)"
        return "Tout — aucune borne de date"
    labels = {
        PERIOD_BETA: "Beta-test",
        PERIOD_LAST_MONTH: "Mois dernier",
        PERIOD_CUSTOM: "Personnalisée",
    }
    label = labels.get(mode, mode)
    return f"{label} — du {_fmt(period[0])} au {_fmt(period[1])}"


def visible_available_groups(available_groups: Iterable[str], configured_groups: Iterable[Mapping[str, object]]) -> list[str]:
    """Return data-backed groups that are visible in the user-group admin."""
    hidden_slugs = {str(group["slug"]) for group in configured_groups if group.get("visible", True) is False}
    return sorted(group for group in available_groups if group and group != "unknown" and group not in hidden_slugs)


def ministry_display_label(value: object) -> str:
    """Readable ministry label for the detailed table and the Excel export.

    Known catalog ids resolve to their label; historic rows without a reliable
    value (NULL/NaN/empty) display « Non renseigné » — never inferred from the
    retrieved sources. Unknown non-empty ids (e.g. eval scopes) pass through.
    """
    if value is None:
        return MINISTRY_NOT_SET_LABEL
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan"}:
        return MINISTRY_NOT_SET_LABEL
    ministry = resolve_ministry(text)
    if ministry is not None:
        return ministry.label
    return text


def _latest_feedback_per_turn(feedbacks: pd.DataFrame) -> pd.DataFrame:
    """Keep one deterministic feedback for each turn.

    The most recent eligible feedback wins. When timestamps tie, the greatest
    database id wins. The dashboard calls this helper after applying its active
    feedback filters, so excluded feedbacks cannot appear in the export.
    """
    if feedbacks.empty or "turn_id" not in feedbacks.columns:
        return feedbacks.iloc[0:0].copy()

    eligible = feedbacks[feedbacks["turn_id"].notna()].copy()
    eligible = eligible[eligible["turn_id"].astype(str).str.strip().ne("")]
    if eligible.empty:
        return eligible

    sort_columns = ["turn_id"]
    ascending = [True]
    if "ts" in eligible.columns:
        eligible["_feedback_sort_ts"] = pd.to_datetime(eligible["ts"], utc=True, errors="coerce")
        sort_columns.append("_feedback_sort_ts")
        ascending.append(False)
    if "id" in eligible.columns:
        sort_columns.append("id")
        ascending.append(False)

    ordered = eligible.sort_values(sort_columns, ascending=ascending, na_position="last", kind="mergesort")
    return ordered.drop_duplicates("turn_id", keep="first")


def _formatted_paris_timestamp(values: pd.Series) -> pd.Series:
    timestamps = pd.to_datetime(values, utc=True, errors="coerce")
    return timestamps.dt.tz_convert(PARIS_TIMEZONE).dt.strftime("%d/%m/%Y %H:%M")


def build_unified_feedback_export(questions: pd.DataFrame, feedbacks: pd.DataFrame) -> pd.DataFrame:
    """Build one row per question, enriched with its latest eligible feedback.

    ``questions`` is the authoritative left side: questions without feedback
    are retained. Both inputs are expected to have already received the active
    dashboard filters. Feedback rows are joined exclusively through ``turn_id``.
    """
    question_fields = [source for source, _ in QUESTION_EXPORT_COLUMNS]
    feedback_fields = [
        "id",
        "ts",
        "turn_id",
        "stars",
        "helpful",
        "reasons_positive",
        "reasons_negative",
        "comment",
        "theme",
        "beta_scope",
        "error_category",
        "ai_reason",
    ]
    output_columns = [label for _, label in (*QUESTION_EXPORT_COLUMNS, *FEEDBACK_EXPORT_COLUMNS)]

    question_data = questions.reindex(columns=question_fields).copy()
    if question_data.empty:
        return pd.DataFrame(columns=output_columns)

    latest_feedbacks = _latest_feedback_per_turn(feedbacks).reindex(columns=feedback_fields)
    latest_feedbacks = latest_feedbacks.rename(
        columns={column: f"feedback_{column}" for column in feedback_fields if column != "turn_id"}
    )
    question_data["turn_id"] = question_data["turn_id"].astype("string")
    latest_feedbacks["turn_id"] = latest_feedbacks["turn_id"].astype("string")

    if latest_feedbacks.empty:
        merged = question_data.copy()
        for column in latest_feedbacks.columns:
            if column != "turn_id":
                merged[column] = pd.NA
    else:
        merged = question_data.merge(latest_feedbacks, on="turn_id", how="left", sort=False, validate="many_to_one")

    export = pd.DataFrame(index=merged.index)
    for source, label in QUESTION_EXPORT_COLUMNS:
        export[label] = merged[source]

    export["Date"] = _formatted_paris_timestamp(merged["ts"])
    export["Ministère"] = merged["selected_ministry"].apply(ministry_display_label)
    export["Thème"] = merged["theme"].combine_first(merged["feedback_theme"])
    export["Date du feedback"] = _formatted_paris_timestamp(merged["feedback_ts"])
    export["Note (1-5)"] = pd.to_numeric(merged["feedback_stars"], errors="coerce") + 1

    for source, label in FEEDBACK_EXPORT_COLUMNS:
        if source not in {"feedback_ts", "feedback_stars"}:
            export[label] = merged[source]

    return export[output_columns]


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
