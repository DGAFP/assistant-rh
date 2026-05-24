"""
Database helpers for the RAG V3 Clean pipeline.

Centralises all DB access: connection management, runtime config (rag_config),
system prompt CRUD (system_prompts), and acronym loading.

Tables managed:
  - rag_config (JSONB, single row) – runtime-tunable parameters
  - system_prompts – editable prompt templates (generator, selector, intent)
  - acronyms – acronym → expansion mapping
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg

logger = logging.getLogger(__name__)

SCALEWAY_ALLOWED_ENVS = {"prod", "staging"}
SCALEWAY_DSN_ENV_KEY = "SCW_POSTGRES_DSN"

DSN_ENV_KEYS = (
    "SCW_POSTGRES_DSN",
    "APP_POSTGRES_DSN",
    "STREAMLIT_POSTGRES_DSN",
    "SCALINGO_POSTGRESQL_URL",
)


# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────


def get_named_dsn(target: str) -> str | None:
    """Return the DSN for a named backend target, or None if unavailable."""
    normalized_target = target.strip().lower()

    if normalized_target == "scaleway":
        env_name = os.getenv("APP_SCALEWAY_ENV", "").strip()
        if not env_name:
            return None
        return get_scaleway_env_dsn(env_name)

    if normalized_target == "scalingo":
        value = os.getenv("SCALINGO_POSTGRESQL_URL", "").strip()
        return value or None

    return None


def get_scaleway_env_dsn(env_name: str) -> str | None:
    """Return the DSN for a named Scaleway environment, or None if unavailable."""
    if env_name.strip().lower() not in SCALEWAY_ALLOWED_ENVS:
        return None
    value = os.getenv(SCALEWAY_DSN_ENV_KEY, "").strip()
    return value or None


def get_dsn() -> str:
    """Return the PostgreSQL connection string from environment variables."""
    target = os.getenv("APP_DB_TARGET", "").strip().lower()
    if target:
        if target == "scalingo":
            dsn = get_named_dsn("scalingo")
            if dsn:
                return dsn
            raise RuntimeError("APP_DB_TARGET=scalingo but SCALINGO_POSTGRESQL_URL is not set.")

        if target == "scaleway":
            env_name = os.getenv("APP_SCALEWAY_ENV", "").strip().lower()

            if env_name not in SCALEWAY_ALLOWED_ENVS:
                raise RuntimeError(
                    "APP_DB_TARGET=scaleway requires APP_SCALEWAY_ENV to be one of: prod, staging (uses SCW_POSTGRES_DSN from environment)."
                )

            dsn = get_scaleway_env_dsn(env_name)
            if dsn:
                return dsn

            env_key = SCALEWAY_DSN_ENV_KEY
            raise RuntimeError(f"APP_DB_TARGET=scaleway with APP_SCALEWAY_ENV={env_name} requires {env_key} to be set.")

        raise RuntimeError(f"Unsupported APP_DB_TARGET={target!r} (expected one of: scalingo, scaleway).")

    dsn = next((os.getenv(key) for key in DSN_ENV_KEYS if os.getenv(key)), "")
    if not dsn:
        raise RuntimeError(f"No database connection string found (set APP_DB_TARGET + canonical DSN, or one of: {', '.join(DSN_ENV_KEYS)}).")
    return dsn


def has_dsn() -> bool:
    """Return True when at least one supported DSN env var is configured."""
    try:
        get_dsn()
        return True
    except RuntimeError:
        return False


def get_sqlalchemy_url() -> str:
    """Return a SQLAlchemy-compatible URL from the configured DSN."""
    url = get_dsn()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _db_conn():
    """Return a psycopg connection or *None*."""
    try:
        return psycopg.connect(get_dsn(), connect_timeout=5)
    except (psycopg.Error, OSError) as exc:
        logger.warning("DB connection failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Runtime config (DB-backed, single-row rag_config table)
# ─────────────────────────────────────────────────────────────────────────────


def get_runtime_config() -> Dict[str, Any]:
    """Read the runtime config dict from *rag_config*; returns ``{}`` on failure."""
    conn = _db_conn()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config FROM rag_config WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {}
    except (psycopg.Error, json.JSONDecodeError):
        return {}
    finally:
        conn.close()


def update_runtime_config(updates: Dict[str, Any], updated_by: str = "admin") -> bool:
    conn = _db_conn()
    if not conn:
        return False
    try:
        current = get_runtime_config()
        current.update(updates)
        current["updated_at"] = datetime.now().isoformat()
        current["updated_by"] = updated_by
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_config SET config = %s, updated_at = CURRENT_TIMESTAMP, updated_by = %s WHERE id = 1",
                (json.dumps(current), updated_by),
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("Config update failed: %s", exc)
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# System prompts (DB-backed, file fallback)
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_TYPES = {
    "generator": "Generateur (reponse finale)",
    "llm_selector": "LLM Selector (filtrage sources)",
    "intent_gating": "Intent Gating (classification)",
}

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def get_prompt_content(name: str) -> Optional[str]:
    """Load a prompt by name: try DB first, then local ``prompts/`` folder."""
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT content FROM system_prompts WHERE name = %s AND is_active = TRUE", (name,))
                row = cur.fetchone()
                if row:
                    content: str = row[0]
                    return content.replace("{today}", datetime.now().strftime("%Y-%m-%d"))
        except psycopg.Error:
            pass
        finally:
            conn.close()

    local = _PROMPTS_DIR / name
    if local.exists():
        content = local.read_text(encoding="utf-8")
        return content.replace("{today}", datetime.now().strftime("%Y-%m-%d"))

    return None


def load_prompt(primary_name: str, fallback_name: str, *, default: Optional[str] = None) -> Optional[str]:
    """Load a prompt by *primary_name*, falling back to *fallback_name*, then *default*.

    Encapsulates the common ``get_prompt_content(x) or get_prompt_content(y) or default``
    pattern used across generator, selector, and query-processor modules.
    """
    return get_prompt_content(primary_name) or get_prompt_content(fallback_name) or default


def list_prompts(prompt_type: str = "generator") -> List[str]:
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM system_prompts WHERE is_active = TRUE AND prompt_type = %s ORDER BY name",
                    (prompt_type,),
                )
                return [r[0] for r in cur.fetchall()]
        except psycopg.Error:
            pass
        finally:
            conn.close()

    if prompt_type == "generator":
        return sorted(p.name for p in _PROMPTS_DIR.glob("*.md"))
    return []


def save_prompt(name: str, content: str, prompt_type: str = "generator", description: str = "", updated_by: str = "admin") -> bool:
    if not name.endswith(".md"):
        name = f"{name}.md"
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO system_prompts (name, content, description, prompt_type, updated_by, updated_at)
                   VALUES (%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
                   ON CONFLICT (name) DO UPDATE SET content=EXCLUDED.content, description=EXCLUDED.description,
                   prompt_type=EXCLUDED.prompt_type, updated_by=EXCLUDED.updated_by, updated_at=CURRENT_TIMESTAMP""",
                (name, content, description, prompt_type, updated_by),
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("save_prompt failed: %s", exc)
        return False
    finally:
        conn.close()


# Alias for backward compatibility
list_system_prompts = list_prompts


# ─────────────────────────────────────────────────────────────────────────────
# Acronyms
# ─────────────────────────────────────────────────────────────────────────────


def get_acronym_dict() -> Dict[str, str]:
    """Load acronyms from the *acronyms* DB table; returns ``{}`` on failure."""
    conn = _db_conn()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT acronym, expansion FROM acronyms ORDER BY priority DESC")
            return {row[0]: row[1] for row in cur.fetchall()}
    except psycopg.Error as exc:
        logger.warning("get_acronym_dict failed: %s", exc)
        return {}
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────


def today_fr() -> str:
    """Return today's date as YYYY-MM-DD (used in prompt placeholders)."""
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy Engine (Streamlit-agnostic)
# ─────────────────────────────────────────────────────────────────────────────


def create_engine_from_env() -> Optional[Any]:
    """Create a SQLAlchemy engine from environment variables.

    Returns None if no DB is configured (no DSN env set).
    Does NOT use Streamlit caching — suitable for scripts, tests, and APIs.
    """
    from sqlalchemy import create_engine, text

    try:
        url = get_sqlalchemy_url()
    except RuntimeError:
        return None
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"

    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=2,
            max_overflow=1,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:
        logger.warning("DB engine creation failed: %s", exc)
        return None


DEFAULT_SYSTEM_PROMPT = "Tu es un assistant RH specialise dans la fonction publique de l'Etat."
