"""
Streaming LLM generator for the RAG V3 Clean pipeline.

Builds the user prompt from context items and streams the response token
by token using ``FallbackLLMClient``.

Dependencies (internal only):
  - config (GenerationConfig)
  - db_helpers (load_prompt)
  - llm_client (FallbackLLMClient)
  - models (ContextItem)
  - context_builder (ContextBuilder – only the static formatter)
"""

from __future__ import annotations

import logging
from typing import Dict, Generator, List

from .config import GenerationConfig
from .context_builder import ContextBuilder
from .db_helpers import load_prompt
from .llm_client import FallbackLLMClient
from .ministry_scope import MinistrySource, render_ministry_prompt
from .models import ContextItem

logger = logging.getLogger(__name__)

USER_PROMPT_TEMPLATE = """Voici le contexte documentaire pour repondre a la question :

{context}

---

**Question de l'utilisateur :** {question}

---

En vous appuyant uniquement sur les sources ci-dessus, repondez de maniere claire et operationnelle.
Si les sources ne permettent pas de repondre, dites-le explicitement et n'inventez pas."""


class StreamingGenerator:
    """
    Generate an answer by streaming tokens from the LLM.

    Usage::

        gen = StreamingGenerator(config)
        for token in gen.stream(query, context_items, history):
            print(token, end="")
    """

    def __init__(self, config: GenerationConfig):
        self.config = config
        self._llm: FallbackLLMClient | None = None
        self._base_prompt: str | None = None
        self.last_full_prompt: str = ""
        self.last_system_prompt: str = ""

    @property
    def llm(self) -> FallbackLLMClient:
        if self._llm is None:
            # Baked default renders the generic (no-ministry) wording; every
            # request overrides it per-call with the selected ministry so a
            # single cached client serves all tenants safely.
            self._llm = FallbackLLMClient(
                primary_provider=self.config.provider.value,
                primary_model=self.config.model,
                fallback_provider=self.config.fallback_provider.value,
                fallback_model=self.config.fallback_model,
                temperature=self.config.temperature,
                system_prompt=self._system_prompt_for(None),
            )
        return self._llm

    def stream(
        self,
        query: str,
        context_items: List[ContextItem],
        history: list[Dict[str, str]] | None = None,
        ministry: MinistrySource | None = None,
    ) -> Generator[str, None, None]:
        """Yield tokens one by one."""
        context_text = ContextBuilder.format_for_prompt(context_items)
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context_text, question=query)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        yield from self.llm.chat_stream(user_prompt, system_prompt=system_prompt, history=history)

    def generate(
        self,
        query: str,
        context_items: List[ContextItem],
        ministry: MinistrySource | None = None,
    ) -> str:
        """Non-streaming variant (useful for evaluation)."""
        context_text = ContextBuilder.format_for_prompt(context_items)
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context_text, question=query)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        return self.llm.chat(user_prompt, system_prompt=system_prompt)

    def _base_system_prompt(self) -> str:
        """Load the (unrendered) system prompt template, cached per instance."""
        if self._base_prompt is None:
            _DEFAULT = (
                "Tu assistes les gestionnaires RH en SGCD pour {ministere_label}. "
                "Reformule les sources pour le gestionnaire, nomme explicitement l'acteur de chaque action "
                "et ne t'adresse jamais à l'agent à la deuxième personne."
            )
            self._base_prompt = load_prompt(self.config.system_prompt_name, "generator.md", default=_DEFAULT)
        return self._base_prompt

    def _system_prompt_for(self, ministry: MinistrySource | None) -> str:
        """Render the system prompt for *ministry* (generic wording if *None*)."""
        return render_ministry_prompt(self._base_system_prompt(), ministry)
