"""
Feedback Dashboard - helpers purs (période effective, libellé ministère).

Extrait de 03_Feedback_Dashboard.py pour être testable sans Streamlit.
"""

from __future__ import annotations

from datetime import date

from assistant_rh_rag_pipeline.ministry_scope import resolve_ministry

BETA_START = date(2026, 1, 8)
BETA_END = date(2026, 2, 6)

PERIOD_ALL = "all"
PERIOD_BETA = "beta"
PERIOD_CUSTOM = "custom"

PERIOD_MODE_OPTIONS = (PERIOD_ALL, PERIOD_BETA, PERIOD_CUSTOM)

PERIOD_MODE_LABELS = {
    PERIOD_ALL: "📅 Tout",
    PERIOD_BETA: "🧪 Beta-test (8 jan – 6 fév 2026)",
    PERIOD_CUSTOM: "🗓️ Personnalisée",
}

MINISTRY_NOT_SET_LABEL = "Non renseigné"


def resolve_period(mode: str, custom_range: tuple | None = None) -> tuple[date, date] | None:
    """Return the (start, end) bounds to apply, or ``None`` for no bound.

    Mode « Tout » returns ``None``: no frozen end date, so data added after the
    view was first opened stays visible on every rerun. The beta-test preset is
    a fixed window; a custom range is only kept when it is complete (2 dates —
    ``st.date_input`` returns a 1-tuple mid-selection).
    """
    if mode == PERIOD_BETA:
        return (BETA_START, BETA_END)
    if mode == PERIOD_CUSTOM and custom_range is not None and len(custom_range) == 2:
        start, end = custom_range
        if start and end:
            return (start, end)
    return None


def period_caption(mode: str, period: tuple[date, date] | None, data_min: date | None = None, data_max: date | None = None) -> str:
    """Human-readable description of the effectively applied period."""

    def _fmt(d: date) -> str:
        return d.strftime("%d/%m/%Y")

    if period is None:
        if data_min is not None and data_max is not None:
            return f"Tout — du {_fmt(data_min)} au {_fmt(data_max)} (suit les nouvelles données)"
        return "Tout — aucune borne de date"
    label = "Beta-test" if mode == PERIOD_BETA else "Personnalisée"
    return f"{label} — du {_fmt(period[0])} au {_fmt(period[1])}"


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
