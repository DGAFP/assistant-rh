"""
Single source of truth for user-group definitions.

Historically the group metadata (slug, label, icon, color, priority) was
duplicated across three places: the chatbot page (``GROUP_PRIORITY`` and
``group_colors``), the admin auth helper (``_GROUP_DISPLAY``) and the feedback
dashboard (``GROUP_COLORS`` / ``GROUP_LABELS``). Adding or renaming a group
meant editing all three in sync.

This module centralises that data. Each consumer rebuilds the exact structure
it used before via the accessor helpers below, so behaviour is unchanged.

The module is intentionally dependency-free (no Streamlit import) so it can be
imported from anywhere. A future change can repoint :data:`GROUPS` at a
database-backed store while keeping these accessors and this seed list as the
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

# Slug of the administrator group (skips the admin password, sees every page).
ADMIN_GROUP = "dgafpallianceadmin"

# Slug used for anonymous sessions before a user selects an authenticated group.
# It must never grant administrator privileges.
DEFAULT_GROUP = "default"

# Fallback display tuple used when a slug is unknown (matches the historical
# ``.get(..., ("👤", "#6b7280", slug))`` defaults at the call sites).
DEFAULT_BADGE = ("👤", "#6b7280")


@dataclass(frozen=True)
class GroupDef:
    """Canonical definition of a user group."""

    slug: str
    label: str  # Badge label, e.g. "DGAFP SD1"
    icon: str  # Badge emoji, e.g. "🏛️"
    color: str  # Badge accent colour (hex)
    priority: int  # Hierarchy: higher wins when resolving group conflicts
    chart_color: str  # Plotly palette colour for the feedback dashboard
    chart_label: str  # Dashboard label, e.g. "🟣 DGAFP SD1"


# Canonical registry, ordered by descending priority.
GROUPS: tuple[GroupDef, ...] = (
    GroupDef("dgafpallianceadmin", "Admin", "🔧", "#6366f1", 100, "#EF553B", "🔴 Admin"),
    GroupDef("dgafpsd1", "DGAFP SD1", "🏛️", "#8b5cf6", 80, "#AB63FA", "🟣 DGAFP SD1"),
    GroupDef("mattecentrale", "MATTE Centrale", "🏢", "#f97316", 70, "#FFA15A", "🟠 MATTE Centrale"),
    GroupDef("mattedreal", "MATTE DREAL", "🌍", "#f97316", 60, "#FFA15A", "🟠 MATTE DREAL"),
    GroupDef("cisirh", "CISIRH", "📊", "#eab308", 50, "#FECB52", "🟡 CISIRH"),
    GroupDef("specloiret", "Loiret", "📍", "#10b981", 40, "#00CC96", "🟢 Loiret"),
    GroupDef("betatest-jan26", "Beta", "🧪", "#3b82f6", 10, "#636EFA", "🔵 Beta Testeurs"),
    GroupDef("default", "Non assigné", "👤", "#6b7280", 0, "#888888", "⚪ Non assigné"),
)

# Dashboard-only pseudo-group for rows whose ``user_group`` is NULL/missing.
# Not a selectable group: it carries chart presentation only.
_UNKNOWN_CHART = ("#888888", "❓ Inconnu")

_BY_SLUG: dict[str, GroupDef] = {g.slug: g for g in GROUPS}


def get_group(slug: str) -> GroupDef | None:
    """Return the :class:`GroupDef` for ``slug``, or ``None`` if unknown."""
    return _BY_SLUG.get(slug)


def group_priority() -> dict[str, int]:
    """Map of slug -> priority (the historical ``GROUP_PRIORITY``)."""
    return {g.slug: g.priority for g in GROUPS}


def valid_groups() -> set[str]:
    """Set of known group slugs (the historical ``VALID_GROUPS``)."""
    return set(_BY_SLUG)


def badge_display() -> dict[str, tuple[str, str, str]]:
    """Map of slug -> (icon, color, label) for the sidebar badge."""
    return {g.slug: (g.icon, g.color, g.label) for g in GROUPS}


def chart_colors() -> dict[str, str]:
    """Map of slug -> Plotly colour for the feedback dashboard."""
    colors = {g.slug: g.chart_color for g in GROUPS}
    colors["unknown"] = _UNKNOWN_CHART[0]
    return colors


def chart_labels() -> dict[str, str]:
    """Map of slug -> dashboard label for the feedback dashboard."""
    labels = {g.slug: g.chart_label for g in GROUPS}
    labels["unknown"] = _UNKNOWN_CHART[1]
    return labels
