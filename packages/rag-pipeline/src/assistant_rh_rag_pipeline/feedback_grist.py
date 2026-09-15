"""Manual dashboard feedback replication; Grist owns human annotations.

No database or Streamlit access here. Records use Grist's server-side upsert,
so an ambiguous timeout can be retried without inserting the feedback twice.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import pandas as pd
import requests

from .ministry_scope import resolve_ministry

# (dashboard field, Grist column, label, type). Keep #470's rating and ministry
# conventions, but retain EVERY feedback, including rows without a chat_run.
SOURCE_COLUMNS = (
    ("ts", "feedback_ts", "Date du feedback (UTC)", "Text"),
    ("turn_id", "turn_id", "turn_id", "Text"),
    ("turn_idx", "turn_idx", "Rang dans la conversation", "Int"),
    ("session_id", "session_id", "session_id", "Text"),
    ("question", "question", "Question", "Text"),
    ("answer", "answer", "Réponse complète", "Text"),
    ("stars", "stars", "Note (1-5)", "Int"),
    ("helpful", "helpful", "Utile (Y/N, vide si absent)", "Text"),
    ("reasons", "reasons", "Raisons historiques", "Text"),
    ("reasons_positive", "reasons_positive", "Raisons positives", "Text"),
    ("reasons_negative", "reasons_negative", "Raisons négatives", "Text"),
    ("comment", "comment", "Commentaire", "Text"),
    ("user_group", "user_group", "Groupe utilisateur", "Text"),
    ("selected_ministry", "selected_ministry", "Identifiant ministère", "Text"),
    ("theme", "theme", "Thème", "Text"),
    ("beta_scope", "beta_scope", "Périmètre bêta (source)", "Text"),
    ("error_category", "error_category", "Catégorie d’erreur (automatique)", "Text"),
    ("ai_reason", "ai_reason", "Justification de l’analyse automatique", "Text"),
    ("ai_analyzed_at", "ai_analyzed_at", "Date de l’analyse (UTC)", "Text"),
    ("rag_version", "rag_version", "Version RAG", "Text"),
    ("chunk_selection_mode", "chunk_selection_mode", "Mode de sélection", "Text"),
    ("dist_after_rerank", "dist_after_rerank", "Sources après reranking", "Text"),
    ("total_time_ms", "total_time_ms", "Durée du run (ms)", "Numeric"),
)
HUMAN_COLUMNS = {"hors_champs": "Hors-champs", "missing_document": "missing_document", "traite": "Traité"}
ENVIRONMENTS = {"local", "staging", "production"}


class FeedbackGristError(RuntimeError):
    """Operator-safe message: never include response bodies, feedbacks or keys."""


@dataclass(frozen=True)
class FeedbackGristConfig:
    base_url: str
    doc_id: str
    table_id: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise FeedbackGristError("GRIST_API_BASE_URL doit être une URL HTTPS sans identifiants, paramètres ou fragment.")
        if not self.doc_id or not self.api_key:
            raise FeedbackGristError("Document et clé API Grist requis.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.table_id):
            raise FeedbackGristError("GRIST_FEEDBACK_TABLE_ID doit être un identifiant de table Grist (lettres, chiffres, underscore).")

    @classmethod
    def from_env(cls) -> FeedbackGristConfig:
        names = ("GRIST_API_BASE_URL", "GRIST_FEEDBACK_DOC_ID", "GRIST_FEEDBACK_TABLE_ID", "GRIST_API_KEY")
        values = [os.getenv(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise FeedbackGristError("Configuration Grist manquante : " + ", ".join(missing))
        config = cls(*values)
        if config.doc_id == os.getenv("GRIST_DOC_ID", "").strip() and config.table_id == os.getenv("GRIST_TABLE_ID", "").strip():
            raise FeedbackGristError("Choisissez une table dédiée aux feedbacks, distincte du référentiel de sources.")
        return config

    @property
    def document_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/doc/{quote(self.doc_id, safe='')}"


def feedback_source_environment() -> str:
    """Use the database deployment environment, including local staging tunnels."""
    environment = (os.getenv("APP_SCALEWAY_ENV") or os.getenv("APP_ENV") or "").strip().lower()
    if environment not in ENVIRONMENTS:
        raise FeedbackGristError("Définissez APP_SCALEWAY_ENV (local, staging ou production) selon la base lue par le dashboard.")
    return environment


def feedback_columns() -> list[dict[str, Any]]:
    definitions = [
        ("environment", "Environnement", "Text"),
        ("feedback_id", "ID du feedback", "Text"),
        *[(column, label, kind) for _, column, label, kind in SOURCE_COLUMNS],
        ("ministry", "Ministère", "Text"),
        *[(column, label, "Bool") for column, label in HUMAN_COLUMNS.items()],
    ]
    # Bool's native empty/default value is False. No default/trigger formula:
    # even new records never need annotation fields in the upsert payload.
    return [{"id": column, "fields": {"label": label, "type": kind, "isFormula": False, "formula": ""}} for column, label, kind in definitions]


def _missing(value: Any) -> bool:
    return not isinstance(value, (list, dict, tuple)) and bool(pd.isna(value))


def _text(value: Any) -> str:
    if _missing(value):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def build_feedback_records(feedbacks: pd.DataFrame, environment: str) -> list[dict[str, Any]]:
    """Map dashboard rows to source-only upserts, validating before any I/O."""
    if environment not in ENVIRONMENTS:
        raise FeedbackGristError("Environnement source invalide.")
    records: dict[str, dict[str, Any]] = {}
    for row in feedbacks.to_dict(orient="records"):
        identifier = row.get("id")
        if _missing(identifier) or isinstance(identifier, bool):
            raise FeedbackGristError("Feedback sans identifiant stable : synchronisation annulée.")
        # A nullable pandas integer column may arrive as 42.0.
        feedback_id = str(int(identifier)) if isinstance(identifier, (int, float)) and int(identifier) == identifier else str(identifier).strip()
        if not feedback_id:
            raise FeedbackGristError("Feedback sans identifiant stable : synchronisation annulée.")
        fields: dict[str, Any] = {}
        for source, column, _, kind in SOURCE_COLUMNS:
            value = row.get(source)
            if source in {"ts", "ai_analyzed_at"}:
                timestamp = pd.to_datetime(value, utc=True, errors="coerce")
                fields[column] = "" if pd.isna(timestamp) else timestamp.isoformat()
            elif source == "helpful":
                fields[column] = "" if _missing(value) else ("Y" if value else "N")
            elif kind in {"Int", "Numeric"}:
                number = pd.to_numeric(value, errors="coerce")
                fields[column] = None if pd.isna(number) else float(number)
                if source == "stars" and fields[column] is not None:
                    fields[column] += 1
            else:
                fields[column] = _text(value)
        ministry_id = fields["selected_ministry"].strip()
        ministry = resolve_ministry(ministry_id)
        fields["ministry"] = ministry.label if ministry else (ministry_id or "Non renseigné")
        record = {"require": {"environment": environment, "feedback_id": feedback_id}, "fields": fields}
        if feedback_id in records and records[feedback_id] != record:
            raise FeedbackGristError("Plusieurs lignes sources différentes pour un même feedback : vérifiez la jointure du dashboard.")
        records[feedback_id] = record
    return list(records.values())


@dataclass(frozen=True)
class FeedbackSyncResult:
    total: int
    synced: int
    error: str | None = None


class FeedbackGristClient:
    def __init__(self, config: FeedbackGristConfig):
        self.config = config

    def _request(self, method: str, resource: str, *, payload: dict | None = None, params: dict | None = None) -> Any:
        url = f"{self.config.base_url.rstrip('/')}/api/docs/{quote(self.config.doc_id, safe='')}/{resource}"
        try:
            response = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
                params=params,
                timeout=(5, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise FeedbackGristError("Grist injoignable ou délai dépassé. Relancez la synchronisation.") from None
        if not 200 <= response.status_code < 300:
            raise FeedbackGristError(f"Grist : HTTP {response.status_code}. Vérifiez la destination et les droits, puis relancez.")
        if method != "GET":
            return None
        try:
            return response.json()
        except ValueError:
            raise FeedbackGristError("Réponse Grist invalide. Relancez la synchronisation.") from None

    def ensure_table(self) -> None:
        """Create the dedicated table once; fail closed on incompatible schemas."""
        payload = self._request("GET", "tables")
        tables = payload.get("tables") if isinstance(payload, dict) else None
        if not isinstance(tables, list) or any(not isinstance(table, dict) or not table.get("id") for table in tables):
            raise FeedbackGristError("Réponse Grist invalide : liste des tables absente ou malformée.")
        if self.config.table_id not in {table["id"] for table in tables}:
            # Do not automatically retry POST: a timeout may have created it.
            # A manual retry first lists tables again.
            self._request("POST", "tables", payload={"tables": [{"id": self.config.table_id, "columns": feedback_columns()}]})
        payload = self._request("GET", f"tables/{self.config.table_id}/columns")
        columns = payload.get("columns") if isinstance(payload, dict) else None
        if not isinstance(columns, list) or any(not isinstance(col, dict) or not isinstance(col.get("fields"), dict) for col in columns):
            raise FeedbackGristError("Réponse Grist invalide : colonnes absentes ou malformées.")
        actual = {column.get("id"): column["fields"] for column in columns}
        for expected in feedback_columns():
            column = actual.get(expected["id"], {})
            if column.get("type") != expected["fields"]["type"] or column.get("isFormula") is not False or column.get("formula"):
                raise FeedbackGristError(f"Colonne Grist incompatible : {expected['id']}. Type attendu : {expected['fields']['type']}, sans formule.")

    def sync(self, feedbacks: pd.DataFrame, environment: str, *, batch_size: int = 100) -> FeedbackSyncResult:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        records = build_feedback_records(feedbacks, environment)
        if not records:
            return FeedbackSyncResult(total=0, synced=0)
        synced = 0
        try:
            self.ensure_table()
            for start in range(0, len(records), batch_size):
                batch = records[start : start + batch_size]
                self._request(
                    "PUT",
                    f"tables/{self.config.table_id}/records",
                    payload={"records": batch},
                    params={"noparse": "true"},
                )
                synced += len(batch)
        except FeedbackGristError as exc:
            return FeedbackSyncResult(total=len(records), synced=synced, error=str(exc))
        return FeedbackSyncResult(total=len(records), synced=synced)
