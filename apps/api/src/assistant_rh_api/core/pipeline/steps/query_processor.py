"""Query processing behind ports, preserving the historical classification rules.

The caller binds ``llm`` to Albert and ``config.intent_model`` (temperature 0),
without a fallback provider: the historical classifier uses LLMClient, not
FallbackLLMClient. ``today`` is the request's already captured local date.
Stores own I/O; all snapshots, errors and inference evidence belong to this call.
"""

import json
import logging
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from assistant_rh_api.core.errors import (
    ClassificationFailure,
    DatabaseConflict,
    DatabaseFailure,
    DatabaseUnavailable,
    InferenceFailure,
    RAGConfigurationError,
)
from assistant_rh_api.core.models.configuration import Acronym, Prompt, Snapshot
from assistant_rh_api.core.models.inference import Attempt, ChatRequest, Completion, Message
from assistant_rh_api.core.models.query_processing import (
    _DIRECT_RESPONSES,
    AVAILABLE_THEMES,
    Intent,
    QueryDiagnostics,
    QueryProcessing,
    QueryProcessResult,
)
from assistant_rh_api.core.models.rag_configuration import QueryProcessorConfig
from assistant_rh_api.core.pipeline.steps.legal_search import should_force_legal_search
from assistant_rh_api.core.ports.configuration import AcronymStorePort, PromptStorePort
from assistant_rh_api.core.ports.inference import LLMPort

logger = logging.getLogger(__name__)


def format_history(history: Sequence[Mapping[str, str]] | None) -> str:
    """Format before loading the prompt, preserving historical error priority."""
    if history and len(history) >= 2:
        parts = []
        for message in history[-8:]:
            role = "Utilisateur" if message["role"] == "user" else "Assistant"
            content = message["content"]
            content = content[:300] + "..." if len(content) > 300 else content
            parts.append(f"{role}: {content}")
        return "\n".join(parts)
    return "(Pas d'historique de conversation)"


def format_acronyms(detected: Mapping[str, str]) -> str:
    return (
        "Les acronymes suivants ont ete detectes (en MAJUSCULES) :\n" + "\n".join(f"- **{short}** = {full}" for short, full in detected.items())
        if detected
        else "(Aucun acronyme detecte)"
    )


def render_prompt(template: str, query: str, history_text: str, acronyms_section: str, ministry: str | None, today: str) -> str:
    """Keep historical ordering and placeholder substitution."""
    # Ministry is the canonical id already resolved/authorized by the caller.
    label = ministry.upper() if ministry else "votre ministère"
    template = template.replace("{today}", today)
    template = template.replace("{ministere_label}", label).replace("{ministere_sigle}", label)
    return template.format(history=history_text, query=query, acronyms_section=acronyms_section)


def _classification_text(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ClassificationFailure("invalid_response") from TypeError("Expected a classification text field")
    return value


def parse_classification(raw: str) -> dict[str, Any]:
    """Keep legacy coercions; translate only invalid JSON or unusable fields."""
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    text = match.group(1) if match else text
    if text.startswith("```"):
        text = text.split("```")[1]
    try:
        data = json.loads(text.strip().lstrip("json").strip())
    except (ValueError, RecursionError) as exc:
        raise ClassificationFailure("invalid_response") from exc
    if not isinstance(data, dict):
        raise ClassificationFailure("invalid_response") from TypeError("Expected a classification object")
    intent_str = data.get("intent", "rag_query")
    if isinstance(intent_str, (list, dict)):
        raise ClassificationFailure("invalid_response") from TypeError("Expected a scalar intent")
    intent = Intent(intent_str) if intent_str in Intent._value2member_map_ else Intent.RAG_QUERY
    theme = data.get("theme")
    if theme and theme not in AVAILABLE_THEMES:
        theme = "autre"
    try:
        confidence = float(data.get("confidence", 0.8))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ClassificationFailure("invalid_response") from exc
    if not math.isfinite(confidence):
        raise ClassificationFailure("invalid_response") from ValueError("Classification confidence must be a finite JSON number")
    return {
        "intent": intent,
        "confidence": confidence,
        "reasoning": _classification_text(data.get("reasoning", "")),
        "needs_legal": bool(data.get("needs_legal_search", False)),
        "theme": _classification_text(theme),
        "enriched_query": _classification_text(data.get("reformulated_query") or ""),
        "query_for_retrieval": _classification_text(data.get("query_for_retrieval") or None),
        "direct_response": _DIRECT_RESPONSES.get(intent),
        "raw": raw,
        "classify_ok": True,
    }


class QueryProcessor:
    def __init__(
        self,
        config: QueryProcessorConfig,
        acronyms: AcronymStorePort,
        prompts: PromptStorePort,
        packaged_prompts: PromptStorePort,
        llm: LLMPort,
    ) -> None:
        self._config = config
        self._acronyms = acronyms
        self._prompts = prompts
        self._packaged_prompts = packaged_prompts
        self._llm = llm

    async def _load_prompt(self, errors: list[str]) -> Snapshot[Prompt]:
        # For each name: DB, then packaged only on absence/DB failure. An empty
        # DB content skips straight to the next name, as get_prompt_content did.
        for name in (self._config.intent_prompt_name, "intent.md"):
            try:
                snapshot = await self._prompts.get(name)
            except (DatabaseFailure, DatabaseUnavailable) as exc:
                errors.append(exc.code)
                snapshot = None
            if snapshot is None:
                snapshot = await self._packaged_prompts.get(name)
            if snapshot is not None and snapshot.value.content:
                return snapshot
        raise FileNotFoundError("Intent prompt not found")

    async def _complete(self, prompt: str) -> Completion:
        try:
            return await self._llm.complete(ChatRequest((Message("user", prompt),), temperature=0.0))
        except InferenceFailure as exc:
            # Rejected requests (credentials/model/payload) and partial outcomes
            # need caller intervention, not a successful degraded classification.
            if exc.partial or any(attempt.error == "rejected" for attempt in exc.attempts):
                raise
            raise ClassificationFailure("provider_failure") from exc

    async def process(
        self,
        query: str,
        conversation_history: Sequence[Mapping[str, str]] | None = None,
        ministry: str | None = None,
        *,
        today: str,
    ) -> QueryProcessing:
        if query:
            query = unicodedata.normalize("NFC", query)
        acronym_snapshot = None
        errors: list[str] = []
        if self._config.enable_acronym_expansion:
            try:
                acronym_snapshot = await self._acronyms.load()
            except (DatabaseConflict, DatabaseFailure, DatabaseUnavailable) as exc:
                errors.append(exc.code)
                logger.warning("Acronym loading failed (%s); continuing query processing without acronyms", exc.code)
        # dict preserves the first ordinal but the last expansion for duplicates.
        acronyms = {a.short: a.expansion for a in acronym_snapshot.value} if acronym_snapshot else {}
        detected = {short: full for short, full in acronyms.items() if re.search(r"\b" + re.escape(short) + r"\b", query)}
        prompt_snapshot = None
        completion = None
        failed_attempts: tuple[Attempt, ...] = ()
        classification_status: Literal["disabled", "completed", "degraded"] = "disabled"
        classification_error: Literal["provider_failure", "invalid_response"] | None = None
        if self._config.enable_intent_gating:
            history_text = format_history(conversation_history)
            acronyms_section = format_acronyms(detected)
            try:
                prompt_snapshot = await self._load_prompt(errors)
            except FileNotFoundError as exc:
                raise RAGConfigurationError() from exc
            try:
                prompt = render_prompt(prompt_snapshot.value.content, query, history_text, acronyms_section, ministry, today)
            except (KeyError, ValueError, IndexError) as exc:
                raise RAGConfigurationError() from exc
            try:
                completion = await self._complete(prompt)
                intent_data = parse_classification(completion.text)
                classification_status = "completed"
            except ClassificationFailure as exc:
                classification_status = "degraded"
                classification_error = exc.reason
                if isinstance(exc.__cause__, InferenceFailure):
                    failed_attempts = exc.__cause__.attempts
                # Retain legacy fallback values, but never expose an exception's
                # message as the reason; it may contain provider/user input.
                intent_data = {"intent": Intent.RAG_QUERY, "confidence": 0.5, "reasoning": exc.reason, "classify_ok": False}
        else:
            expanded = query
            for short, full in detected.items():
                expanded = re.sub(r"\b" + re.escape(short) + r"\b", f"{short} ({full})", expanded)
            intent_data = {"intent": Intent.RAG_QUERY, "confidence": 1.0, "query_for_retrieval": expanded if expanded != query else None}

        processed = intent_data.get("query_for_retrieval") or query
        llm_needs_legal = intent_data.get("needs_legal")
        if self._config.enable_intent_gating and intent_data.get("classify_ok", False):
            needs_legal = should_force_legal_search(query=query, processed_query=processed, intent_data=intent_data)
        else:
            needs_legal = bool(llm_needs_legal)
        expanded_acronyms = tuple(short for short, full in detected.items() if short in processed and full in processed)
        result = QueryProcessResult(
            original_query=query,
            processed_query=processed,
            enriched_query=intent_data.get("enriched_query", ""),
            expanded_acronyms=expanded_acronyms,
            detected_acronyms=tuple(Acronym(short, full) for short, full in detected.items()),
            was_expanded=bool(expanded_acronyms),
            is_in_scope=intent_data["intent"] in (Intent.RAG_QUERY, Intent.FOLLOW_UP),
            intent=intent_data["intent"],
            intent_confidence=intent_data.get("confidence", 1.0),
            intent_reason=intent_data.get("reasoning"),
            needs_legal_search=needs_legal,
            needs_legal_search_llm=llm_needs_legal,
            theme=intent_data.get("theme"),
            was_enriched=bool(intent_data.get("enriched_query")),
            direct_response=intent_data.get("direct_response"),
            intent_raw_response=intent_data.get("raw"),
        )
        return QueryProcessing(
            result,
            QueryDiagnostics(
                acronyms=acronym_snapshot,
                prompt=prompt_snapshot,
                completion=completion,
                failed_attempts=failed_attempts,
                store_errors=tuple(errors),
                classification_status=classification_status,
                classification_error=classification_error,
            ),
        )
