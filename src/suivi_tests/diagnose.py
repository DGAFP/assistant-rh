"""Diagnostic par étape d'un run ``chat_runs`` face aux documents attendus.

Pour chaque document attendu (pattern de titre), on cherche la dernière étape
du pipeline où il apparaît encore :

    retrieval (v3_chunks_raw)
      → rerank (v3_chunks_after_rerank)
      → agregation (v3_context_items_full, les ~20 sections proposées au selector)
      → selector (v3_selector_kept_indices)
      → context builder (budget et limite de sections)
      → sources (retrieved, les sources finales affichées)

Les fonctions sont pures (dict → dataclass) : elles acceptent indifféremment
une ligne SQL de ``chat_runs`` ou le dict produit par
``chat_logger.build_log_row`` (mêmes clés, valeurs JSON déjà sérialisées ou
non).
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

STAGES = ("retrieval", "rerank", "agregation", "selector", "context_builder", "sources")

# Verdict par pattern : "ok" ou l'étape où le document attendu disparaît.
VERDICT_LABELS = {
    "ok": "OK",
    "absent_retrieval": "absent du retrieval",
    "perdu_rerank": "perdu au rerank",
    "perdu_agregation": "perdu à l'agrégation (hors top sections)",
    "ecarte_selector": "écarté par le selector",
    "perdu_context_builder": "perdu au ContextBuilder (budget/limite)",
}


def _norm(value: str | None) -> str:
    """Minuscules sans accents, pour un matching insensible à la saisie."""
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()


def _parse_json_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def parse_kept_indices(value: Any) -> list[int]:
    """``v3_selector_kept_indices`` est stocké en CSV (« 1,5 ») ou liste."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value if str(item).strip().isdigit()]
    return [int(part) for part in str(value).split(",") if part.strip().isdigit()]


def _chunk_haystack(chunk: Mapping[str, Any]) -> str:
    return _norm(str(chunk.get("doc_title") or "")) + " | " + _norm(str(chunk.get("section_heading") or ""))


def _alternatives(pattern: str) -> list[str]:
    """Un pattern peut contenir des alternatives « A|B » : l'une OU l'autre
    porte le document attendu (ex. deux docs couvrant le même acte)."""
    return [_norm(part) for part in pattern.split("|") if part.strip()]


def _matches(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)


def _first_hit(chunks: list[dict[str, Any]], needles: list[str]) -> tuple[int, dict[str, Any]] | None:
    for index, chunk in enumerate(chunks):
        if _matches(_chunk_haystack(chunk), needles):
            return index, chunk
    return None


@dataclass
class PatternDiagnosis:
    """Trajet d'un document attendu à travers les étapes du pipeline."""

    pattern: str
    verdict: str  # clé de VERDICT_LABELS
    retrieval_rank: int | None = None
    retrieval_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    context_index: int | None = None
    kept_by_selector: bool = False
    in_final_sources: bool = False

    @property
    def label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)


@dataclass
class RunDiagnosis:
    """Diagnostic complet d'un run pour tous ses documents attendus."""

    patterns: list[PatternDiagnosis] = field(default_factory=list)

    @property
    def overall(self) -> str:
        """« ok » seulement si TOUS les documents attendus atteignent les
        sources finales (les entrées de la liste sont un ET ; les alternatives
        « A|B » d'une même entrée, un OU). Sinon, le verdict du document
        manquant allé le plus loin dans le pipeline — l'étape la plus proche
        de la sortie est la plus actionnable."""
        if not self.patterns:
            return "non_evalue"
        order = ["absent_retrieval", "perdu_rerank", "perdu_agregation", "ecarte_selector", "perdu_context_builder", "ok"]
        failing = [diag.verdict for diag in self.patterns if diag.verdict != "ok"]
        if not failing:
            return "ok"
        return max(failing, key=order.index)

    @property
    def overall_label(self) -> str:
        return VERDICT_LABELS.get(self.overall, self.overall)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def diagnose_pattern(
    pattern: str,
    *,
    raw: list[dict[str, Any]],
    reranked: list[dict[str, Any]],
    context_items: list[dict[str, Any]],
    kept_indices: list[int],
    final_sources: list[dict[str, Any]],
    doc_titles: dict[str, str],
) -> PatternDiagnosis:
    needles = _alternatives(pattern)
    diag = PatternDiagnosis(pattern=pattern, verdict="absent_retrieval")

    raw_hit = _first_hit(raw, needles)
    if raw_hit is not None:
        diag.retrieval_rank = raw_hit[0] + 1
        diag.retrieval_score = _to_float(raw_hit[1].get("final_score"))

    rerank_hit = _first_hit(reranked, needles)
    if rerank_hit is not None:
        diag.rerank_rank = rerank_hit[0] + 1
        diag.rerank_score = _to_float(rerank_hit[1].get("rerank_score"))

    # Les context items (sections agrégées) ne portent pas le titre du doc :
    # on le résout via document_id → doc_title vu dans les chunks.
    for index, item in enumerate(context_items):
        title = doc_titles.get(str(item.get("document_id") or ""), "")
        haystack = _norm(title) + " | " + _norm(str(item.get("heading") or ""))
        if _matches(haystack, needles):
            if diag.context_index is None:
                diag.context_index = index
            if index in kept_indices:
                diag.kept_by_selector = True
                break

    for source in final_sources:
        haystack = _norm(str(source.get("source_name") or "")) + " | " + _norm(str(source.get("doc_title") or ""))
        if _matches(haystack, needles):
            diag.in_final_sources = True
            break

    if diag.in_final_sources:
        diag.verdict = "ok"
    elif diag.kept_by_selector:
        diag.verdict = "perdu_context_builder"
    elif diag.context_index is not None:
        diag.verdict = "ecarte_selector"
    elif diag.rerank_rank is not None:
        diag.verdict = "perdu_agregation"
    elif diag.retrieval_rank is not None:
        diag.verdict = "perdu_rerank"
    else:
        diag.verdict = "absent_retrieval"
    return diag


def diagnose_run(row: Mapping[str, Any], expected_patterns: list[str]) -> RunDiagnosis:
    """Diagnostique une ligne ``chat_runs`` (ou un dict ``build_log_row``)."""
    raw = _parse_json_list(row.get("v3_chunks_raw"))
    reranked = _parse_json_list(row.get("v3_chunks_after_rerank"))
    context_items = _parse_json_list(row.get("v3_context_items_full"))
    final_sources = _parse_json_list(row.get("retrieved"))
    kept_indices = parse_kept_indices(row.get("v3_selector_kept_indices"))

    doc_titles: dict[str, str] = {}
    for chunk in (*raw, *reranked):
        doc_id = str(chunk.get("doc_id") or "")
        if doc_id and doc_id not in doc_titles:
            doc_titles[doc_id] = str(chunk.get("doc_title") or "")

    return RunDiagnosis(
        patterns=[
            diagnose_pattern(
                pattern,
                raw=raw,
                reranked=reranked,
                context_items=context_items,
                kept_indices=kept_indices,
                final_sources=final_sources,
                doc_titles=doc_titles,
            )
            for pattern in expected_patterns
        ]
    )


def format_diagnosis_line(identifier: str, question: str, diagnosis: RunDiagnosis) -> str:
    """Une ligne lisible par question pour le rapport texte."""
    status = "✓" if diagnosis.overall == "ok" else "✗"
    parts = [f"{status} {identifier:<12} {diagnosis.overall_label:<42} {question[:70]}"]
    if diagnosis.overall != "ok":
        for diag in diagnosis.patterns:
            details: list[str] = []
            if diag.retrieval_rank is not None:
                details.append(f"retrieval#{diag.retrieval_rank}")
            if diag.rerank_rank is not None:
                details.append(f"rerank#{diag.rerank_rank} ({diag.rerank_score})")
            if diag.context_index is not None:
                details.append(f"context item#{diag.context_index}")
            trail = " → ".join(details) if details else "jamais remonté"
            parts.append(f"    · « {diag.pattern} » : {diag.label} [{trail}]")
    return "\n".join(parts)
