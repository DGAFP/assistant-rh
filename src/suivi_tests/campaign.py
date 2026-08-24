"""Chargement de la campagne Grist « Suivi-Tests » et des documents attendus.

La table ``Manuel_testing`` porte les questions (``question``, ``doc_ref``) ;
les documents attendus par question viennent, au choix :

1. d'une colonne Grist ``expected_docs`` (un pattern par ligne) si elle existe
   — c'est la voie cible, éditable par le testeur ;
2. sinon du fichier de config versionné (``config/suivi_tests_expected_docs.json``,
   ``{record_id: [patterns]}``), qui encode les attendus de la campagne du
   08/07/2026.

Un « pattern » est un fragment de titre de document, matché sans accents ni
casse contre ``doc_title``/``source_name``/``section_heading``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Doc Grist « Suivi-Tests » (org assistantrhinterne) — distinct du GRIST_DOC_ID
# par défaut du .env, qui pointe le référentiel des sources.
DEFAULT_GRIST_DOC_ID = "uL3Jf7TJTx2uRmjpTnZGTf"
DEFAULT_GRIST_TABLE_ID = "Manuel_testing"
EXPECTED_DOCS_COLUMN = "expected_docs"
DEFAULT_EXPECTED_CONFIG = Path("config/suivi_tests_expected_docs.json")


@dataclass
class CampaignQuestion:
    record_id: int
    question: str
    doc_ref: str
    ministry_id: str
    expected_patterns: list[str] = field(default_factory=list)

    @property
    def conversation_id(self) -> str:
        return f"{self.ministry_id}-{self.record_id}"


def resolve_ministry_id(doc_ref: str | None) -> str:
    """``doc_ref`` Grist → id ministère du catalog.

    Même convention que ``resolve_question_scope`` de l'eval goldset : les
    référentiels non ministériels (SP, LEGI, general…) suivent le parcours
    MATTE (matte + tables partagées SP/Légifrance, présentes dans tout scope).
    """
    from assistant_rh_rag_pipeline.ministry_scope import MINISTRY_CATALOG

    ministry_id = (doc_ref or "").strip().lower()
    return ministry_id if ministry_id in MINISTRY_CATALOG else "matte"


def load_expected_docs(path: Path) -> dict[int, list[str]]:
    """Charge le mapping ``{record_id: [patterns]}`` depuis le fichier JSON."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[int, list[str]] = {}
    for key, patterns in payload.items():
        if str(key).startswith("_"):
            continue  # clés de commentaire
        mapping[int(key)] = [str(pattern) for pattern in patterns if str(pattern).strip()]
    return mapping


def _split_patterns(value: Any) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).replace(";", "\n").splitlines() if part.strip()]


def fetch_campaign_questions(
    *,
    grist_doc_id: str | None = None,
    grist_table_id: str | None = None,
    expected_config: Path | None = None,
    only_ids: set[int] | None = None,
) -> list[CampaignQuestion]:
    """Lit les questions de la campagne dans Grist et attache les attendus.

    Les patterns de la colonne Grist ``expected_docs`` (si présente) priment
    sur le fichier de config pour une même question.
    """
    from assistant_rh_data_engineering.utils.grist import GristClient, GristConfig

    doc_id = grist_doc_id or os.getenv("SUIVI_TESTS_GRIST_DOC_ID") or DEFAULT_GRIST_DOC_ID
    table_id = grist_table_id or DEFAULT_GRIST_TABLE_ID
    config = GristConfig(
        base_url=os.getenv("GRIST_API_BASE_URL", "").rstrip("/"),
        api_key=os.getenv("GRIST_API_KEY", ""),
        doc_id=doc_id,
        table_id=table_id,
    )
    client = GristClient(config)

    has_expected_column = EXPECTED_DOCS_COLUMN in client.list_columns(table_id)
    expected_from_config = load_expected_docs(expected_config or DEFAULT_EXPECTED_CONFIG)

    questions: list[CampaignQuestion] = []
    for record in client.list_records(table_id):
        record_id = int(record.get("id", 0))
        fields = record.get("fields", {}) or {}
        question_text = str(fields.get("question") or "").strip()
        if not question_text:
            continue
        if only_ids is not None and record_id not in only_ids:
            continue
        expected = _split_patterns(fields.get(EXPECTED_DOCS_COLUMN)) if has_expected_column else []
        if not expected:
            expected = expected_from_config.get(record_id, [])
        doc_ref = str(fields.get("doc_ref") or "").strip()
        questions.append(
            CampaignQuestion(
                record_id=record_id,
                question=question_text,
                doc_ref=doc_ref,
                ministry_id=resolve_ministry_id(doc_ref),
                expected_patterns=expected,
            )
        )
    return questions
