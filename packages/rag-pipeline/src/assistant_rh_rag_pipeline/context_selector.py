"""
LLM-based context selector for the RAG V3 Clean pipeline.

When enabled (``SelectorConfig.enabled = True``), an LLM reviews the list of
candidate sections post-rerank and drops irrelevant ones before they reach
the ContextBuilder.

When **disabled** (default), ``select()`` is a no-op – all sections pass through.

Dependencies (internal only):
  - config (SelectorConfig, get_prompt_content)
  - llm_client (LLMClient)
  - models (AggregatedSection, ContextItem)
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Union

from .config import SelectorConfig
from .db_helpers import load_prompt
from .llm_client import LLMClient
from .ministry_scope import MinistrySource, render_ministry_prompt
from .models import AggregatedSection, ContextItem

logger = logging.getLogger(__name__)

SelectorItem = Union[AggregatedSection, ContextItem]


_COMPLEMENTARY_SOURCE_SELECTION_RULE = """

## Redondance et complémentarité

Avant d'éliminer une section comme redondante, identifie l'information précise
qu'elle apporte. Deux sections sont redondantes uniquement si elles donnent la
même règle, la même condition ou la même modalité sans apport supplémentaire.

Elles sont complémentaires si chacune apporte un élément distinct utile à la
réponse : champ d'application, conditions, modalités, autorité compétente,
consultation requise, texte de mise en œuvre ou déclinaison ministérielle.
Le fait de traiter du même sujet, ou qu'une source soit prioritaire, ne suffit
jamais à rendre une autre source redondante.

Applique le même test de pertinence à tous les éditeurs. La hiérarchie des
sources sert uniquement à départager deux passages réellement équivalents.
Garde toutes les sections directement pertinentes dont l'apport est distinct.
"""


@dataclass
class _ParseResult:
    """Distinguish between successful parse (even if empty) and parse failure."""

    ids: List[int] = field(default_factory=list)
    is_explicit_empty: bool = False


class ContextSelector:
    """
    Stateful LLM-based selector that filters sections by relevance.

    Each ``select()`` call stores results internally so the pipeline can
    inspect them afterwards via properties, without relying on module globals.

    Usage::

        selector = ContextSelector(config.selector)
        kept = selector.select(query, sections)
        print(selector.last_decisions)
        print(selector.all_rejected)
    """

    def __init__(self, config: SelectorConfig):
        self._config = config
        self._last_decisions: dict = {}
        self._last_raw_response: str = ""
        self._last_reasoning: str = ""
        self._last_prompt_chars: int = 0

    def _reset(self) -> None:
        self._last_decisions = {}
        self._last_raw_response = ""
        self._last_reasoning = ""
        self._last_prompt_chars = 0

    # ── Public properties ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Whether the selector is active."""
        return self._config.enabled

    @property
    def last_decisions(self) -> dict:
        """Selector decisions: ``{kept: [...], removed: [...], reason: "..."}``."""
        return self._last_decisions

    @property
    def last_raw_response(self) -> str:
        """Raw LLM response from the last ``select()`` call."""
        return self._last_raw_response

    @property
    def last_reasoning(self) -> str:
        """Extracted reasoning from the last ``select()`` call."""
        return self._last_reasoning

    @property
    def last_prompt_chars(self) -> int:
        """Character count of the exact user prompt sent to the selector LLM."""
        return self._last_prompt_chars

    @property
    def all_rejected(self) -> bool:
        """True if the LLM explicitly rejected every section."""
        return self._last_decisions.get("all_rejected", False)

    # ── Main entry point ───────────────────────────────────────────────

    def select(
        self,
        query: str,
        sections: List[AggregatedSection],
        ministry: MinistrySource | None = None,
    ) -> List[AggregatedSection]:
        """
        Filter *sections* through the LLM selector.

        Returns the kept subset. If disabled or on failure, returns all
        *sections* unchanged.
        """
        self._reset()

        if not self._config.enabled or not sections:
            return sections

        try:
            llm = LLMClient(
                provider=self._config.provider.value,
                model=self._config.model,
                temperature=self._config.temperature,
            )

            prompt_template = load_prompt(
                self._config.prompt_name,
                "selector.md",
                default=_DEFAULT_PROMPT,
            ) or _DEFAULT_PROMPT
            # DB-backed prompts can lag behind the versioned fallback. Apply
            # the same redundancy test to every request and every publisher;
            # DGAFP is already part of the always-on retrieval pool.
            publishers = {str(section.publisher or "").strip().casefold() for section in sections if section.publisher}
            if len(publishers) > 1 and "## Redondance et complémentarité" not in prompt_template:
                prompt_template = f"{prompt_template.rstrip()}\n{_COMPLEMENTARY_SOURCE_SELECTION_RULE}"
            # Resolve {ministere_*} before format_map fills {query}/{context}.
            prompt_template = render_ministry_prompt(prompt_template, ministry)

            numbered = []
            for i, sec in enumerate(sections):
                label = f"[{i}] {sec.heading} ({sec.publisher or 'unknown'})"
                numbered.append(f"{label}\n{sec.markdown}")

            format_vars = {
                "query": query,
                "context": "\n\n---\n\n".join(numbered),
                "theme": "",
            }
            try:
                prompt = prompt_template.format_map(defaultdict(str, format_vars))
            except Exception:
                prompt = prompt_template.replace("{query}", query).replace("{context}", "\n\n---\n\n".join(numbered)).replace("{theme}", "")

            self._last_prompt_chars = len(prompt)
            raw = llm.chat(prompt, system_prompt="")
            self._last_raw_response = raw
            parsed = _parse_response(raw, len(sections))
            reason = _parse_reason(raw)
            self._last_reasoning = reason

            if parsed.is_explicit_empty:
                logger.info(
                    "Selector explicitly rejected all %d sections – reason: %s",
                    len(sections),
                    reason[:120],
                )
                self._last_decisions = {
                    "kept": [],
                    "removed": [
                        {"idx": i, "heading": (sections[i].heading or "")[:80], "publisher": sections[i].publisher or ""}
                        for i in range(len(sections))
                    ],
                    "reason": reason,
                    "all_rejected": True,
                }
                return []

            if not parsed.ids:
                _FALLBACK_K = 5
                logger.warning("Selector parse failure – keeping top %d sections", _FALLBACK_K)
                return sections[:_FALLBACK_K]

            # Dédup en préservant l'ordre: un LLM peut répéter un indice, ce qui
            # servirait deux fois la même section au générateur (et la listerait
            # deux fois dans la trace kept).
            selected_ids = list(dict.fromkeys(i for i in parsed.ids if 0 <= i < len(sections)))
            # Le plancher est appliqué AVANT de figer la trace kept/removed:
            # les sections repêchées sont réellement servies au générateur et
            # doivent apparaître dans "kept" (chat_logger et les pages d'audit
            # Streamlit lisent ces listes comme le contexte servi).
            served_ids = self._top_up_ids(selected_ids, len(sections))
            served_set = set(served_ids)
            removed_ids = [i for i in range(len(sections)) if i not in served_set]
            self._last_decisions = {
                "kept": [{"idx": i, "heading": (sections[i].heading or "")[:80], "publisher": sections[i].publisher or ""} for i in served_ids],
                "removed": [{"idx": i, "heading": (sections[i].heading or "")[:80], "publisher": sections[i].publisher or ""} for i in removed_ids],
                "reason": reason,
            }
            if len(served_ids) > len(selected_ids):
                self._last_decisions["topped_up_to_min"] = {
                    "floor": self._config.min_kept_sections,
                    "selected_by_llm": len(selected_ids),
                    "served": len(served_ids),
                }

            filtered = [sections[i] for i in served_ids]
            logger.info("Selector kept %d / %d sections", len(filtered), len(sections))
            return filtered if filtered else sections

        except Exception as exc:
            logger.warning("Context selector failed (%s), keeping all sections", exc)
            return sections

    def _top_up_ids(self, selected_ids: List[int], total: int) -> List[int]:
        """Complète la sélection jusqu'au plancher ``min_kept_sections``.

        Le sélecteur LLM élague le bruit mais ne doit pas affamer le
        générateur: quand il garde quelque chose, on remonte au plancher avec
        les sections suivantes au rang d'agrégation (la liste arrive triée par
        score). Le rejet explicite total (all_rejected) n'est pas concerné —
        il pilote la logique de retry du pipeline et doit rester vide.
        """
        floor = self._config.min_kept_sections or 0
        if not selected_ids or len(selected_ids) >= floor:
            return selected_ids
        served = list(selected_ids)
        seen = set(served)
        for i in range(total):
            if len(served) >= floor:
                break
            if i not in seen:
                served.append(i)
                seen.add(i)
        return served

    def select_context(
        self,
        query: str,
        items: List[ContextItem],
    ) -> List[ContextItem]:
        """Convenience wrapper: select over ``ContextItem`` lists."""
        light_sections = [
            AggregatedSection(
                section_id=it.section_id,
                heading=it.heading,
                markdown=it.content,
                chunks=[],
                score=it.score,
                publisher=it.publisher,
                references_juridiques=it.references_juridiques,
                metadata=it.metadata,
            )
            for it in items
        ]
        kept = self.select(query, light_sections)
        kept_ids = {id(s) for s in kept}
        return [items[i] for i, s in enumerate(light_sections) if id(s) in kept_ids]


# ── Pure helper functions (stateless) ──────────────────────────────────


def _parse_response(raw: str, n_items: int) -> _ParseResult:
    """Extract selected indices from the LLM JSON response."""
    try:
        data = _extract_json(raw)
        raw_ids = data.get("selected_ids") or data.get("selected_indices") or data.get("selected_ordered")
        if raw_ids is None or (isinstance(raw_ids, list) and len(raw_ids) == 0):
            return _ParseResult(is_explicit_empty=True)
        out = []
        for v in raw_ids:
            if isinstance(v, int):
                out.append(v)
            elif isinstance(v, str):
                digits = re.sub(r"[^0-9]", "", v)
                if digits:
                    out.append(int(digits))
        return _ParseResult(ids=[i for i in out if 0 <= i < n_items])
    except (ValueError, KeyError, json.JSONDecodeError):
        return _ParseResult()


def _parse_reason(raw: str) -> str:
    """Extract the 'reason' field from the LLM JSON response."""
    try:
        data = _extract_json(raw)
        return data.get("reason", "")
    except (ValueError, KeyError, json.JSONDecodeError):
        return ""


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    text = m.group(1) if m else text
    return json.loads(text.strip())


_DEFAULT_PROMPT = """Tu es un expert en selection de contexte pour un assistant RH.

**Question :** {query}

**Sections disponibles :**
{context}

Selectionne les sections pertinentes pour repondre a la question.

Reponds UNIQUEMENT avec un JSON :
```json
{{
  "selected_ids": [0, 2, 5],
  "reason": "Explication courte"
}}
```
"""
