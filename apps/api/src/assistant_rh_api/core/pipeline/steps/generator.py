"""C5 generation policy, independent of provider implementations and HTTP.

The composition root binds llm to config.provider/model followed by
config.fallback_provider/model (Albert then Scaleway by default), using B3's
pre-content-only fallback. Non-stream generation preserves the legacy absence
of history; streaming keeps every supplied history message in order. C6 owns
retrieval retry before passing the final all_rejected flag; C7 owns SSE transport.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace

from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.models.generation import GenerationDiagnostics, GenerationResult
from assistant_rh_api.core.models.inference import Message, StreamCompleted, TextDelta
from assistant_rh_api.core.models.rag_configuration import GenerationConfig
from assistant_rh_api.core.ports.configuration import PromptStorePort
from assistant_rh_api.core.ports.inference import LLMPort
from assistant_rh_api.core.prompt_policy import DEFAULT_GENERATOR_PROMPT, NO_ANSWER, generation_request, load_prompt


class Generator:
    def __init__(self, config: GenerationConfig, prompts: PromptStorePort, packaged_prompts: PromptStorePort, llm: LLMPort) -> None:
        self._config = config
        self._prompts = prompts
        self._packaged_prompts = packaged_prompts
        self._llm = llm

    async def _prepare(
        self,
        query: str,
        items: Sequence[ContextItem],
        ministry: str | None,
        history: tuple[Message, ...] = (),
        *,
        today: str,
    ) -> GenerationDiagnostics:
        loaded = await load_prompt(self._prompts, self._packaged_prompts, self._config.system_prompt_name, "generator.md", DEFAULT_GENERATOR_PROMPT)
        request = generation_request(loaded.snapshot.value.content, query, items, ministry, self._config.temperature, history, today=today)
        return GenerationDiagnostics("completed", loaded.snapshot, request, store_errors=loaded.store_errors)

    async def generate(
        self,
        query: str,
        items: Sequence[ContextItem],
        ministry: str | None = None,
        *,
        today: str,
        all_rejected: bool = False,
    ) -> GenerationResult:
        if not items and all_rejected:
            return GenerationResult(NO_ANSWER, GenerationDiagnostics("no_answer"))
        diagnostics = await self._prepare(query, tuple(items), ministry, today=today)
        assert diagnostics.request is not None
        outcome = await self._llm.complete(diagnostics.request)
        return GenerationResult(outcome.text, replace(diagnostics, outcome=outcome))

    @asynccontextmanager
    async def stream(
        self,
        query: str,
        items: Sequence[ContextItem],
        history: tuple[Message, ...] = (),
        ministry: str | None = None,
        *,
        today: str,
        all_rejected: bool = False,
    ) -> AsyncIterator[AsyncIterator[TextDelta | GenerationResult]]:
        if not items and all_rejected:

            async def no_answer() -> AsyncIterator[TextDelta | GenerationResult]:
                yield TextDelta(NO_ANSWER)
                yield GenerationResult(NO_ANSWER, GenerationDiagnostics("no_answer"))

            yield no_answer()
            return
        diagnostics = await self._prepare(query, tuple(items), ministry, tuple(history), today=today)
        assert diagnostics.request is not None
        # Exiting the consumer's context always closes the injected stream,
        # including an early break and cancellation.
        async with self._llm.stream(diagnostics.request) as events:

            async def responses() -> AsyncIterator[TextDelta | GenerationResult]:
                collected: list[str] = []
                async for event in events:
                    if isinstance(event, StreamCompleted):
                        yield GenerationResult("".join(collected), replace(diagnostics, outcome=event))
                        return
                    collected.append(event.text)
                    yield event
                # A broken port must not turn an incomplete response into success.
                raise InferenceFailure((), partial=bool(collected))

            yield responses()
