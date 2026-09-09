"""
Admin CRUD operations for the RAG V3 Clean module.

Provides database-backed management of:
  - Runtime RAG configuration (rag_config table)
  - System prompts (system_prompts table)
  - Acronyms (acronyms table)

Used primarily by pages/04_Admin_Config.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg

from .config import RAGConfig, freeze_config
from .db_helpers import (
    _db_conn,
    get_prompt_content,
    get_runtime_config,
    get_sqlalchemy_url,
    list_prompts,
    save_prompt,
)
from .runtime_config import RuntimeRAGConfig
from .runtime_config import runtime_config_to_rag_config as map_runtime_config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Runtime RAG config (rag_config, single-row JSONB)
# ─────────────────────────────────────────────────────────────────────────────


DEFAULT_CONFIG = freeze_config(RuntimeRAGConfig())

VALIDATION_RULES: Dict[str, Dict[str, Any]] = {
    "rag_version": {"choices": ["v1", "v2", "v3"], "type": str},
    "chunk_selection_mode": {"choices": ["llm_selector"], "type": str},
    # TODO(pr212-review): "narrow" is stale — ContextMode has no NARROW, so a value
    # of "narrow" passes validation then silently coerces to STANDARD in
    # runtime_config_to_rag_config's mode_map. Drop "narrow" here (or add the enum).
    "v3_context_mode": {"choices": ["narrow", "standard", "wide"], "type": str},
    "v3_search_mode": {"choices": ["semantic", "hybrid", "lexical"], "type": str},
    "v3_token_budget": {"min": 2000, "max": 20000, "type": int},
    "v3_initial_top_k": {"min": 3, "max": 30, "type": int},
    "v3_temperature": {"min": 0.0, "max": 2.0, "type": float},
    "top_k": {"min": 1, "max": 50, "type": int},
    "rerank_top_k": {"min": 1, "max": 20, "type": int},
    "hybrid_alpha": {"min": 0.0, "max": 1.0, "type": float},
    "temperature": {"min": 0.0, "max": 2.0, "type": float},
    "retrieval_mode": {"choices": ["semantic", "lexical", "hybrid"], "type": str},
    "llm_provider": {"choices": ["albert", "scaleway", "mistral"], "type": str},
    "data_source": {"choices": ["all", "all_sans_rgrh", "matte", "rgrh", "service_public", "dgafp", "csv"], "type": str},
    "embedding_model": {"choices": ["albert", "bge_scaleway", "qwen3_scaleway"], "type": str},
    "embedding_fallback": {"choices": ["albert", "bge_scaleway", "qwen3_scaleway", "none"], "type": str},
}


def validate_config(updates: Dict[str, Any]) -> Dict[str, str]:
    errors: Dict[str, str] = {}
    for fld, value in updates.items():
        rules = VALIDATION_RULES.get(fld)
        if not rules:
            continue
        expected = rules.get("type")
        if expected and not isinstance(value, expected):
            try:
                value = expected(value)
                updates[fld] = value
            except (ValueError, TypeError):
                errors[fld] = f"Doit etre de type {expected.__name__}"
                continue
        if "min" in rules and value < rules["min"]:
            errors[fld] = f"Minimum: {rules['min']}"
        elif "max" in rules and value > rules["max"]:
            errors[fld] = f"Maximum: {rules['max']}"
        if "choices" in rules and value not in rules["choices"]:
            errors[fld] = f"Valeurs possibles: {', '.join(rules['choices'])}"
    return errors


def init_config_table() -> bool:
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rag_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    config JSONB NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by VARCHAR(100) DEFAULT 'system',
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("SELECT COUNT(*) FROM rag_config")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO rag_config (id, config) VALUES (1, %s)",
                    (json.dumps(DEFAULT_CONFIG.to_dict()),),
                )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.warning("Config table init failed: %s", exc)
        return False
    finally:
        conn.close()


def get_rag_config() -> RuntimeRAGConfig:
    raw = get_runtime_config()
    if raw:
        return RuntimeRAGConfig.from_dict(raw)
    return RuntimeRAGConfig()


def runtime_config_to_rag_config(runtime_config: RuntimeRAGConfig | None = None) -> RAGConfig:
    """Compatibility entry point: omitted config still reads the existing DB."""
    return map_runtime_config(runtime_config if runtime_config is not None else get_rag_config())


def update_rag_config(updated_by: str = "admin", **kwargs) -> tuple[bool, Dict[str, str]]:
    errors = validate_config(kwargs)
    if errors:
        return False, errors
    current = get_rag_config().to_dict()
    current.update(kwargs)
    current["updated_at"] = datetime.now().isoformat()
    current["updated_by"] = updated_by
    conn = _db_conn()
    if not conn:
        return False, {"_db": "Base de donnees non disponible"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_config SET config = %s, updated_at = CURRENT_TIMESTAMP, updated_by = %s WHERE id = 1",
                (json.dumps(current), updated_by),
            )
            conn.commit()
        return True, {}
    except psycopg.Error as exc:
        logger.error("Config update failed: %s", exc)
        return False, {"_db": str(exc)}
    finally:
        conn.close()


def reset_to_defaults(updated_by: str = "admin") -> bool:
    d = DEFAULT_CONFIG.to_dict()
    d["updated_at"] = datetime.now().isoformat()
    d["updated_by"] = updated_by
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_config SET config = %s, updated_at = CURRENT_TIMESTAMP, updated_by = %s WHERE id = 1",
                (json.dumps(d), updated_by),
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("Config reset failed: %s", exc)
        return False
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# System prompts extended CRUD
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SystemPrompt:
    name: str
    content: str
    description: str = ""
    prompt_type: str = "generator"
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""
    updated_by: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemPrompt":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid})


def init_prompts_table() -> bool:
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_prompts (
                    name VARCHAR(100) PRIMARY KEY,
                    content TEXT NOT NULL,
                    description VARCHAR(500) DEFAULT '',
                    prompt_type VARCHAR(50) DEFAULT 'generator',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by VARCHAR(100) DEFAULT 'system'
                )
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'system_prompts' AND column_name = 'prompt_type'
                    ) THEN
                        ALTER TABLE system_prompts ADD COLUMN prompt_type VARCHAR(50) DEFAULT 'generator';
                    END IF;
                END $$;
            """)
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.warning("Prompts table init failed: %s", exc)
        return False
    finally:
        conn.close()


# Re-export list_system_prompts under its old name
list_system_prompts = list_prompts


def get_all_prompts(prompt_type: Optional[str] = None) -> List[SystemPrompt]:
    conn = _db_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            if prompt_type:
                cur.execute(
                    """
                    SELECT name, content, description, prompt_type, is_active,
                           created_at::text, updated_at::text, updated_by
                    FROM system_prompts WHERE prompt_type = %s ORDER BY name
                """,
                    (prompt_type,),
                )
            else:
                cur.execute("""
                    SELECT name, content, description, prompt_type, is_active,
                           created_at::text, updated_at::text, updated_by
                    FROM system_prompts ORDER BY prompt_type, name
                """)
            return [
                SystemPrompt(
                    name=r[0],
                    content=r[1],
                    description=r[2] or "",
                    prompt_type=r[3] or "generator",
                    is_active=r[4],
                    created_at=r[5] or "",
                    updated_at=r[6] or "",
                    updated_by=r[7] or "system",
                )
                for r in cur.fetchall()
            ]
    except psycopg.Error as exc:
        logger.warning("get_all_prompts failed: %s", exc)
        return []
    finally:
        conn.close()


def delete_prompt(name: str) -> tuple[bool, str]:
    if name == "system_prompt.md":
        return False, "Impossible de supprimer le prompt par defaut"
    conn = _db_conn()
    if not conn:
        return False, "Base de donnees non disponible"
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_prompts SET is_active = FALSE WHERE name = %s", (name,))
            conn.commit()
        return True, f"Prompt '{name}' desactive"
    except psycopg.Error as exc:
        return False, str(exc)
    finally:
        conn.close()


def duplicate_prompt(source_name: str, new_name: str, updated_by: str = "admin") -> tuple[bool, str]:
    content = get_prompt_content(source_name)
    if not content:
        return False, f"Prompt source '{source_name}' introuvable"
    prompt_type = "generator"
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT prompt_type FROM system_prompts WHERE name = %s", (source_name,))
                row = cur.fetchone()
                if row:
                    prompt_type = row[0] or "generator"
        except psycopg.Error:
            pass
        finally:
            conn.close()
    return save_prompt(new_name, content, prompt_type, f"Copie de {source_name}", updated_by)


# ─────────────────────────────────────────────────────────────────────────────
# Acronyms CRUD
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Acronym:
    acronym: str
    expansion: str
    category: str = "general"
    description: str = ""
    priority: int = 1
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sqlalchemy_url() -> Optional[str]:
    """Return a SQLAlchemy-compatible URL or None."""
    try:
        return get_sqlalchemy_url()
    except RuntimeError:
        return None


def init_acronyms_table() -> bool:
    url = _sqlalchemy_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS acronyms (
                    id SERIAL PRIMARY KEY,
                    acronym TEXT UNIQUE NOT NULL,
                    expansion TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    description TEXT DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.warning("Acronyms table init failed: %s", exc)
        return False


def get_all_acronyms() -> List[Acronym]:
    url = _sqlalchemy_url()
    if not url:
        return []
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                SELECT acronym, expansion, category, description,
                       COALESCE(priority, 1) as priority, created_at, updated_at
                FROM acronyms ORDER BY acronym, priority
            """)
            )
            return [
                Acronym(
                    acronym=r[0],
                    expansion=r[1],
                    category=r[2] or "general",
                    description=r[3] or "",
                    priority=r[4] or 1,
                    created_at=str(r[5]) if r[5] else "",
                    updated_at=str(r[6]) if r[6] else "",
                )
                for r in result
            ]
    except psycopg.Error as exc:
        logger.warning("get_all_acronyms failed: %s", exc)
        return []


def add_acronym(acronym: str, expansion: str, category: str = "general", description: str = "") -> bool:
    url = _sqlalchemy_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(
                text("""
                INSERT INTO acronyms (acronym, expansion, category, description)
                VALUES (:acronym, :expansion, :category, :description)
                ON CONFLICT (acronym) DO UPDATE SET
                    expansion = EXCLUDED.expansion,
                    category = EXCLUDED.category,
                    description = EXCLUDED.description,
                    updated_at = NOW()
            """),
                {
                    "acronym": acronym.upper().strip(),
                    "expansion": expansion.strip(),
                    "category": (category or "general").strip(),
                    "description": (description or "").strip(),
                },
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("add_acronym failed: %s", exc)
        return False


def update_acronym(acronym: str, expansion: str, category: str = None, description: str = None) -> bool:
    url = _sqlalchemy_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            updates = ["expansion = :expansion", "updated_at = NOW()"]
            params: Dict[str, Any] = {"acronym": acronym.upper().strip(), "expansion": expansion.strip()}
            if category is not None:
                updates.append("category = :category")
                params["category"] = category.strip()
            if description is not None:
                updates.append("description = :description")
                params["description"] = description.strip()
            conn.execute(text(f"UPDATE acronyms SET {', '.join(updates)} WHERE acronym = :acronym"), params)
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("update_acronym failed: %s", exc)
        return False


def delete_acronym(acronym: str) -> bool:
    url = _sqlalchemy_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM acronyms WHERE acronym = :acronym"), {"acronym": acronym.upper().strip()})
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("delete_acronym failed: %s", exc)
        return False


def get_categories() -> List[str]:
    url = _sqlalchemy_url()
    if not url:
        return ["general"]
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                SELECT DISTINCT category FROM acronyms
                WHERE category IS NOT NULL AND category != ''
                ORDER BY category
            """)
            )
            return [r[0] for r in result]
    except psycopg.Error as exc:
        logger.warning("get_categories failed: %s", exc)
        return ["general"]


def get_missing_acronyms(limit: int = 50, min_occurrences: int = 1) -> List[Dict]:
    url = _sqlalchemy_url()
    if not url:
        return []
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                SELECT acronym, query, first_seen_at, last_seen_at,
                       occurrence_count, added_to_acronyms, notes
                FROM acronyms_missing
                WHERE occurrence_count >= :min_occ AND added_to_acronyms = FALSE
                ORDER BY occurrence_count DESC, last_seen_at DESC
                LIMIT :lim
            """),
                {"min_occ": min_occurrences, "lim": limit},
            )
            return [
                {
                    "acronym": r[0],
                    "sample_query": r[1],
                    "first_seen_at": str(r[2]) if r[2] else "",
                    "last_seen_at": str(r[3]) if r[3] else "",
                    "occurrence_count": r[4],
                    "added_to_acronyms": r[5],
                    "notes": r[6] or "",
                }
                for r in result
            ]
    except psycopg.Error as exc:
        logger.warning("get_missing_acronyms failed: %s", exc)
        return []


def mark_acronym_as_added(acronym: str) -> bool:
    url = _sqlalchemy_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE acronyms_missing SET added_to_acronyms = TRUE WHERE acronym = :acronym"),
                {"acronym": acronym.upper().strip()},
            )
            conn.commit()
        return True
    except psycopg.Error as exc:
        logger.error("mark_acronym_as_added failed: %s", exc)
        return False
