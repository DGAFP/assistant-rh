"""
Feedback Dashboard - helpers purs (période effective, libellé ministère).

Extrait de 03_Feedback_Dashboard.py pour être testable sans Streamlit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

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
