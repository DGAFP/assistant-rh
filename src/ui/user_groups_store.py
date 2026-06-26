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
import logging
import os
import secrets
from typing import Any

import psycopg
from assistant_rh_rag_pipeline.db_helpers import get_dsn

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


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_groups (
    slug          VARCHAR(64) PRIMARY KEY,
    label         VARCHAR(128) NOT NULL,
    icon          VARCHAR(16)  NOT NULL DEFAULT '',
    color         VARCHAR(16)  NOT NULL DEFAULT '',
    priority      INTEGER      NOT NULL DEFAULT 0,
    password_hash TEXT,
    is_admin      BOOLEAN      NOT NULL DEFAULT FALSE,
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
            for g in GROUPS:
                is_admin = g.slug == ADMIN_GROUP
                raw = admin_pwd if is_admin else default_pwd
                pwd_hash = hash_password(raw) if raw else None
                cur.execute(
                    """
                    INSERT INTO user_groups
                        (slug, label, icon, color, priority, password_hash, is_admin, chart_color, chart_label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slug) DO NOTHING
                    """,
                    (g.slug, g.label, g.icon, g.color, g.priority, pwd_hash, is_admin, g.chart_color, g.chart_label),
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
                SELECT slug, label, icon, color, priority, is_admin,
                       chart_color, chart_label, (password_hash IS NOT NULL) AS has_password
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
