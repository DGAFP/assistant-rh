"""
Database-backed store for user groups.

Mirrors the single-table admin pattern used by ``rag_config`` (see
``assistant_rh_rag_pipeline.admin``): a private connection helper plus an
idempotent ``init_user_groups_table()`` that creates the table and seeds it
on first run.

The canonical *seed* (the 8 historical groups and their display metadata)
lives in :mod:`src.ui.groups`. This module persists those groups in Postgres,
adds password hashing, and exposes read/verify helpers for the homepage
picker. When the database is unavailable, reads fall back to the in-memory
seed so the UI degrades gracefully instead of crashing.

Passwords are hashed with stdlib ``hashlib.pbkdf2_hmac`` (no extra dependency)
and stored as ``pbkdf2_<algo>$<iterations>$<salt_b64>$<hash_b64>``.

Seed passwords:
  - the admin group is seeded from ``ADMIN_PASSWORD``;
  - every other group is seeded from the shared ``GROUP_DEFAULT_PASSWORD``.
A group whose seed password env var is empty is created with no password
(``password_hash IS NULL``) and cannot be logged into until an admin sets one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from typing import Any

import psycopg
from assistant_rh_rag_pipeline.db_helpers import get_dsn
from assistant_rh_rag_pipeline.ministry_scope import (
    MinistryScopeError,
    RetrievalScope,
    build_retrieval_scope,
    known_ministry_ids,
)
from psycopg.types.json import Jsonb

from src.ui.groups import ADMIN_GROUP, GROUPS

logger = logging.getLogger(__name__)

_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 200_000


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing (stdlib pbkdf2)
# ─────────────────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return a ``pbkdf2_<algo>$<iters>$<salt_b64>$<hash_b64>`` string."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _verify_hash(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored pbkdf2 string."""
    if not stored:
        return False
    try:
        algo_label, iters_str, salt_b64, hash_b64 = stored.split("$")
        algo = algo_label.removeprefix("pbkdf2_")
        iterations = int(iters_str)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────


def _conn():
    """Return a psycopg connection, or ``None`` if the DB is unavailable."""
    try:
        return psycopg.connect(get_dsn(), connect_timeout=5)
    except (psycopg.Error, OSError, RuntimeError) as exc:
        logger.warning("user_groups DB connection failed: %s", exc)
        return None


def db_available() -> bool:
    """Return True when the user_groups DB is reachable.

    ``verify_password`` returns False both for a wrong password and for an
    unreachable DB; callers use this to tell those cases apart (e.g. to show
    a "service unavailable" message instead of "wrong password").
    """
    conn = _conn()
    if not conn:
        return False
    conn.close()
    return True


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_groups (
    slug          VARCHAR(64) PRIMARY KEY,
    label         VARCHAR(128) NOT NULL,
    icon          VARCHAR(16)  NOT NULL DEFAULT '',
    color         VARCHAR(16)  NOT NULL DEFAULT '',
    priority      INTEGER      NOT NULL DEFAULT 0,
    password_hash TEXT,
    is_admin      BOOLEAN      NOT NULL DEFAULT FALSE,
    visible       BOOLEAN      NOT NULL DEFAULT TRUE,
    allowed_ministries JSONB    NOT NULL DEFAULT '["matte"]'::jsonb,
    default_ministry   TEXT     NOT NULL DEFAULT 'matte',
    chart_color   VARCHAR(16)  NOT NULL DEFAULT '',
    chart_label   VARCHAR(64)  NOT NULL DEFAULT '',
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
)
"""


def init_user_groups_table() -> bool:
    """Create the ``user_groups`` table and seed missing groups.

    Idempotent: existing rows are never overwritten (``ON CONFLICT DO NOTHING``),
    so an admin's later password/label changes survive re-seeding.
    """
    conn = _conn()
    if not conn:
        return False
    admin_pwd = os.getenv("ADMIN_PASSWORD", "").strip()
    default_pwd = os.getenv("GROUP_DEFAULT_PASSWORD", "").strip()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            # Forward migration for tables created before the `visible` column.
            cur.execute("ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS visible BOOLEAN NOT NULL DEFAULT TRUE")
            cur.execute("ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS allowed_ministries JSONB NOT NULL DEFAULT '[\"matte\"]'::jsonb")
            cur.execute("ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS default_ministry TEXT NOT NULL DEFAULT 'matte'")
            # Only seed groups not already present: hashing is expensive
            # (pbkdf2, 200k iters) and ``ON CONFLICT DO NOTHING`` would throw
            # the work away for every already-seeded group on each new session.
            cur.execute("SELECT slug FROM user_groups")
            existing = {row[0] for row in cur.fetchall()}
            for g in GROUPS:
                if g.slug in existing:
                    continue
                is_admin = g.slug == ADMIN_GROUP
                raw = admin_pwd if is_admin else default_pwd
                pwd_hash = hash_password(raw) if raw else None
                cur.execute(
                    """
                    INSERT INTO user_groups
                        (slug, label, icon, color, priority, password_hash, is_admin, allowed_ministries, default_ministry, chart_color, chart_label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slug) DO NOTHING
                    """,
                    (g.slug, g.label, g.icon, g.color, g.priority, pwd_hash, is_admin, Jsonb(["matte"]), "matte", g.chart_color, g.chart_label),
                )
        conn.commit()
        return True
    except psycopg.Error as exc:
        logger.warning("user_groups table init failed: %s", exc)
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────


def _seed_fallback() -> list[dict[str, Any]]:
    """Build the group list from the in-memory seed (used when DB is down)."""
    admin_pwd = bool(os.getenv("ADMIN_PASSWORD", "").strip())
    default_pwd = bool(os.getenv("GROUP_DEFAULT_PASSWORD", "").strip())
    rows: list[dict[str, Any]] = []
    for g in GROUPS:
        is_admin = g.slug == ADMIN_GROUP
        rows.append(
            {
                "slug": g.slug,
                "label": g.label,
                "icon": g.icon,
                "color": g.color,
                "priority": g.priority,
                "is_admin": is_admin,
                "visible": True,
                "allowed_ministries": ["matte"],
                "default_ministry": "matte",
                "chart_color": g.chart_color,
                "chart_label": g.chart_label,
                "has_password": admin_pwd if is_admin else default_pwd,
            }
        )
    return rows


def list_groups() -> list[dict[str, Any]]:
    """Return all groups (highest priority first) with a ``has_password`` flag.

    Falls back to the in-memory seed when the database is unavailable.
    ``password_hash`` itself is never returned.
    """
    conn = _conn()
    if not conn:
        return _seed_fallback()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT slug, label, icon, color, priority, is_admin, visible,
                       allowed_ministries, default_ministry, chart_color, chart_label,
                       (password_hash IS NOT NULL) AS has_password
                FROM user_groups
                ORDER BY priority DESC, slug ASC
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except psycopg.Error as exc:
        logger.warning("user_groups list failed: %s", exc)
        return _seed_fallback()
    finally:
        conn.close()


def _parse_ministries(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []

    out: list[str] = []
    for item in value:
        ministry_id = str(item or "").strip().lower()
        if ministry_id and ministry_id not in out:
            out.append(ministry_id)
    return out


def validate_ministry_policy(allowed_ministries: Any, default_ministry: Any) -> tuple[bool, str, list[str], str]:
    """Validate a group ministry policy against the code-owned ministry catalog."""
    allowed = _parse_ministries(allowed_ministries)
    default = str(default_ministry or "").strip().lower()
    known = known_ministry_ids()

    if not allowed:
        return False, "Au moins un ministère autorisé est requis.", [], default

    unknown = [ministry_id for ministry_id in allowed if ministry_id not in known]
    if unknown:
        return False, f"Ministère inconnu dans la politique: {', '.join(unknown)}.", allowed, default

    if not default:
        return False, "Le ministère par défaut est requis.", allowed, default
    if default not in known:
        return False, f"Ministère par défaut inconnu: {default}.", allowed, default
    if default not in allowed:
        return False, "Le ministère par défaut doit faire partie des ministères autorisés.", allowed, default

    return True, "", allowed, default


def group_policy_status(group: dict[str, Any]) -> dict[str, Any]:
    """Return normalized ministry policy status for a group row."""
    ok, error, allowed, default = validate_ministry_policy(
        group.get("allowed_ministries"),
        group.get("default_ministry"),
    )
    return {
        "valid": ok,
        "error": error,
        "allowed_ministries": allowed,
        "default_ministry": default,
    }


def get_group_policy(slug: str) -> dict[str, Any]:
    """Return one group's ministry policy, fail-closed when missing or invalid."""
    slug = (slug or "").strip().lower()
    for group in list_groups():
        if group["slug"] == slug:
            policy = group_policy_status(group)
            return {"slug": slug, **policy}
    return {
        "slug": slug,
        "valid": False,
        "error": f"Groupe « {slug} » introuvable.",
        "allowed_ministries": [],
        "default_ministry": "",
    }


def resolve_group_retrieval_scope(group_slug: str, selected_ministry: str) -> tuple[RetrievalScope | None, str]:
    """Resolve a group + selected ministry to a strict RAG retrieval scope."""
    policy = get_group_policy(group_slug)
    if not policy["valid"]:
        return None, str(policy["error"])

    selected = (selected_ministry or policy["default_ministry"] or "").strip().lower()
    if selected not in policy["allowed_ministries"]:
        return None, "Le ministère sélectionné n'est pas autorisé pour ce groupe."

    try:
        return build_retrieval_scope(selected), ""
    except MinistryScopeError as exc:
        return None, str(exc)


def group_chart_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Return (colors, labels) keyed by slug for the feedback dashboard.

    DB-authoritative (with seed fallback via :func:`list_groups`), so groups an
    admin created render with their own colour/label instead of the generic
    grey/"unknown" defaults. The dashboard-only ``unknown`` pseudo-group (rows
    whose ``user_group`` is NULL) is always included.
    """
    rows = list_groups()
    colors = {g["slug"]: (g.get("chart_color") or "#888888") for g in rows}
    labels = {g["slug"]: (g.get("chart_label") or g["label"]) for g in rows}
    colors["unknown"] = "#888888"
    labels["unknown"] = "❓ Inconnu"
    return colors, labels


def group_badge_display() -> dict[str, tuple[str, str, str]]:
    """Return slug -> (icon, color, label) for the sidebar badge.

    DB-authoritative (with seed fallback via :func:`list_groups`).
    """
    return {g["slug"]: (g["icon"], g["color"], g["label"]) for g in list_groups()}


def group_priorities() -> dict[str, int]:
    """Map of slug -> priority, DB-authoritative with seed fallback.

    Unlike the seed ``groups.group_priority()``, this includes admin-created
    groups, so the chatbot resolver gives them their real priority instead of 0.
    """
    return {g["slug"]: g["priority"] for g in list_groups()}


def known_group_slugs() -> set[str]:
    """Set of all known group slugs, DB-authoritative with seed fallback."""
    return {g["slug"] for g in list_groups()}


def is_admin_group(slug: str) -> bool:
    """Return True when ``slug`` is flagged as an admin group in the store.

    Falls back to comparing against the seed ``ADMIN_GROUP`` slug when the DB is
    unavailable or the group is not yet persisted, preserving the historical
    behaviour.
    """
    if not slug:
        return False
    conn = _conn()
    if not conn:
        return slug == ADMIN_GROUP
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM user_groups WHERE slug = %s", (slug,))
            row = cur.fetchone()
        if row is None:
            return slug == ADMIN_GROUP
        return bool(row[0])
    except psycopg.Error as exc:
        logger.warning("user_groups is_admin check failed: %s", exc)
        return slug == ADMIN_GROUP
    finally:
        conn.close()


def verify_password(slug: str, password: str) -> bool:
    """Return True when ``password`` matches the stored hash for ``slug``."""
    if not slug or not password:
        return False
    conn = _conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM user_groups WHERE slug = %s", (slug,))
            row = cur.fetchone()
        return _verify_hash(password, row[0]) if row else False
    except psycopg.Error as exc:
        logger.warning("user_groups verify failed: %s", exc)
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Writes (admin CRUD)
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_EDITABLE_FIELDS = (
    "label",
    "icon",
    "color",
    "priority",
    "is_admin",
    "visible",
    "allowed_ministries",
    "default_ministry",
    "chart_color",
    "chart_label",
)

# Seeded structural groups that must not be deleted: init re-seeds any missing
# seed slug on the next load, so a "delete" would silently reappear. Retire them
# with the `visible` toggle instead. Admin-created groups remain deletable.
PROTECTED_SLUGS = {g.slug for g in GROUPS}


def create_group(
    slug: str,
    label: str,
    password: str,
    *,
    icon: str = "👥",
    color: str = "#6b7280",
    priority: int = 0,
    is_admin: bool = False,
    visible: bool = True,
    allowed_ministries: list[str] | None = None,
    default_ministry: str = "matte",
    chart_color: str = "#888888",
    chart_label: str = "",
) -> tuple[bool, str]:
    """Create a new group. Every group requires a password. Returns (ok, error)."""
    slug = (slug or "").strip().lower()
    label = (label or "").strip()
    if not _SLUG_RE.match(slug):
        return False, "Slug invalide : minuscules, chiffres et tirets, 2 à 64 caractères."
    if not label:
        return False, "Le libellé est requis."
    if not password:
        return False, "Un mot de passe est requis."
    if priority < 0:
        return False, "La priorité doit être ≥ 0."
    candidate_allowed = ["matte"] if allowed_ministries is None else allowed_ministries
    ok, policy_error, normalized_allowed, normalized_default = validate_ministry_policy(candidate_allowed, default_ministry)
    if not ok:
        return False, policy_error
    chart_label = (chart_label or "").strip() or f"{icon} {label}".strip()
    conn = _conn()
    if not conn:
        return False, "Base de données indisponible."
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM user_groups WHERE slug = %s", (slug,))
            if cur.fetchone():
                return False, f"Le groupe « {slug} » existe déjà."
            cur.execute(
                """
                INSERT INTO user_groups
                    (slug, label, icon, color, priority, password_hash, is_admin, visible,
                     allowed_ministries, default_ministry, chart_color, chart_label)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    slug,
                    label,
                    icon,
                    color,
                    priority,
                    hash_password(password),
                    is_admin,
                    visible,
                    Jsonb(normalized_allowed),
                    normalized_default,
                    chart_color,
                    chart_label,
                ),
            )
        conn.commit()
        return True, ""
    except psycopg.Error as exc:
        logger.warning("create_group failed: %s", exc)
        return False, f"Erreur base de données : {exc}"
    finally:
        conn.close()


def update_group(slug: str, **fields: Any) -> tuple[bool, str]:
    """Update editable metadata for a group (not the password). Returns (ok, error)."""
    slug = (slug or "").strip().lower()
    updates = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS}
    if not updates:
        return False, "Aucun champ à mettre à jour."
    if "label" in updates and not str(updates["label"]).strip():
        return False, "Le libellé est requis."
    if "priority" in updates and int(updates["priority"]) < 0:
        return False, "La priorité doit être ≥ 0."
    if "allowed_ministries" in updates:
        updates["allowed_ministries"] = _parse_ministries(updates["allowed_ministries"])
    if "default_ministry" in updates:
        updates["default_ministry"] = str(updates["default_ministry"] or "").strip().lower()

    conn = _conn()
    if not conn:
        return False, "Base de données indisponible."
    try:
        if "allowed_ministries" in updates or "default_ministry" in updates:
            with conn.cursor() as cur:
                cur.execute("SELECT allowed_ministries, default_ministry FROM user_groups WHERE slug = %s", (slug,))
                row = cur.fetchone()
            if row is None:
                return False, f"Groupe « {slug} » introuvable."
            candidate_allowed = updates.get("allowed_ministries", row[0])
            candidate_default = updates.get("default_ministry", row[1])
            ok, policy_error, normalized_allowed, normalized_default = validate_ministry_policy(candidate_allowed, candidate_default)
            if not ok:
                return False, policy_error
            updates["allowed_ministries"] = Jsonb(normalized_allowed)
            updates["default_ministry"] = normalized_default

        # Column names come only from the _EDITABLE_FIELDS whitelist; values are parameterised.
        set_clause = ", ".join(f"{k} = %s" for k in updates) + ", updated_at = CURRENT_TIMESTAMP"
        params = [*updates.values(), slug]
        with conn.cursor() as cur:
            cur.execute(f"UPDATE user_groups SET {set_clause} WHERE slug = %s", params)
            if cur.rowcount == 0:
                return False, f"Groupe « {slug} » introuvable."
        conn.commit()
        return True, ""
    except psycopg.Error as exc:
        logger.warning("update_group failed: %s", exc)
        return False, f"Erreur base de données : {exc}"
    finally:
        conn.close()


def set_password(slug: str, password: str) -> tuple[bool, str]:
    """Reset a group's password. Returns (ok, error)."""
    if not password:
        return False, "Un mot de passe est requis."
    slug = (slug or "").strip().lower()
    conn = _conn()
    if not conn:
        return False, "Base de données indisponible."
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_groups SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE slug = %s",
                (hash_password(password), slug),
            )
            if cur.rowcount == 0:
                return False, f"Groupe « {slug} » introuvable."
        conn.commit()
        return True, ""
    except psycopg.Error as exc:
        logger.warning("set_password failed: %s", exc)
        return False, f"Erreur base de données : {exc}"
    finally:
        conn.close()


def delete_group(slug: str) -> tuple[bool, str]:
    """Delete a group. Structural groups (default, admin) are protected. Returns (ok, error)."""
    slug = (slug or "").strip().lower()
    if slug in PROTECTED_SLUGS:
        return False, f"Le groupe « {slug} » est protégé et ne peut pas être supprimé."
    conn = _conn()
    if not conn:
        return False, "Base de données indisponible."
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_groups WHERE slug = %s", (slug,))
            if cur.rowcount == 0:
                return False, f"Groupe « {slug} » introuvable."
        conn.commit()
        return True, ""
    except psycopg.Error as exc:
        logger.warning("delete_group failed: %s", exc)
        return False, f"Erreur base de données : {exc}"
    finally:
        conn.close()
