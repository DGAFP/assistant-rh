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

En vous appuyant uniquement sur les sources ci-dessus, repondez de maniere claire, precise et proportionnee a la question.
Pour une question sur les regles, exposez le cadre documente sans ajouter de rubrique "En pratique" ni de procedure pas-a-pas.
N'inferez pas la frequence d'une decision, qui choisit une modalite, ni une demarche locale si les sources ne le disent pas explicitement.
Si les sources ne permettent pas de repondre, dites-le explicitement et n'inventez pas."""

_COMPLEMENTARY_SOURCE_COVERAGE = """## Couverture des sources complémentaires

Lorsque le contexte contient à la fois un texte juridique et une source
ministérielle directement pertinents, utilisez les deux : présentez d'abord le
cadre légal général, puis distinguez clairement sa mise en œuvre ministérielle.
N'écartez pas une disposition juridique non redondante au seul motif qu'une
fiche pratique est disponible, et n'ajoutez aucune consigne opérationnelle qui
n'est pas étayée par les sources. N'en déduisez pas de procédure locale (jour
imposé, démarche auprès du service RH ou circuit de validation) si elle n'est
pas explicitement décrite. Une question sur les règles ne demande pas une
procédure pas-à-pas : n'ajoutez une marche à suivre que si l'utilisateur la
demande explicitement. Nommez les articles juridiques pertinents lorsque leur
numéro figure dans le contexte et ne désignez jamais les documents par des
numéros techniques comme « source 1 » ou « document 2 ». N'inférez jamais la
fréquence d'une décision ni qui choisit une modalité lorsque les sources ne le
précisent pas."""


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
        *,
        seed: int | None = None,
    ) -> Generator[str, None, None]:
        """Yield tokens one by one."""
        context_text = ContextBuilder.format_for_prompt(context_items)
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context_text, question=query)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        if seed is None:
            yield from self.llm.chat_stream(user_prompt, system_prompt=system_prompt, history=history)
        else:
            yield from self.llm.chat_stream(user_prompt, system_prompt=system_prompt, history=history, seed=seed)

    def generate(
        self,
        query: str,
        context_items: List[ContextItem],
        ministry: MinistrySource | None = None,
        *,
        seed: int | None = None,
    ) -> str:
        """Non-streaming variant (useful for evaluation)."""
        context_text = ContextBuilder.format_for_prompt(context_items)
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context_text, question=query)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        if seed is None:
            return self.llm.chat(user_prompt, system_prompt=system_prompt)
        return self.llm.chat(user_prompt, system_prompt=system_prompt, seed=seed)

    def _base_system_prompt(self) -> str:
        """Load the (unrendered) system prompt template, cached per instance."""
        if self._base_prompt is None:
            _DEFAULT = "Tu es un assistant RH expert pour {ministere_label}. Reponds aux questions des agents publics sur les ressources humaines."
            prompt = load_prompt(self.config.system_prompt_name, "generator.md", default=_DEFAULT) or _DEFAULT
            # The configured prompt is DB-backed and may predate deployed
            # code. Enforce this conditional invariant in code as well as in
            # the versioned prompt so stale configuration cannot hide law.
            if "## Couverture des sources complémentaires" not in prompt:
                prompt = f"{prompt.rstrip()}\n\n{_COMPLEMENTARY_SOURCE_COVERAGE}"
            self._base_prompt = prompt
        return self._base_prompt

    def _system_prompt_for(self, ministry: MinistrySource | None) -> str:
        """Render the system prompt for *ministry* (generic wording if *None*)."""
        return render_ministry_prompt(self._base_system_prompt(), ministry)
