"""
Unified LLM client for the RAG V3 Clean pipeline.

Wraps OpenAI-compatible APIs (Albert, Scaleway, OpenAI) behind a single
interface with automatic fallback: if the primary provider fails *before*
yielding any streaming token, the secondary provider is tried.

Used by: query_processor, context_selector, generator.

Environment variables (at least one pair required):
  ALBERT_API_KEY  + ALBERT_BASE_URL
  SCALEWAY_API_KEY + SCALEWAY_BASE_URL
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Dict, Generator, Optional

logger = logging.getLogger(__name__)

_PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "albert": {"key_env": "ALBERT_API_KEY", "url_env": "ALBERT_BASE_URL"},
    "scaleway": {"key_env": "SCALEWAY_API_KEY", "url_env": "SCALEWAY_BASE_URL"},
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "url_env": "OPENROUTER_BASE_URL"},
    "openai": {"key_env": "OPENAI_API_KEY", "url_env": ""},
}

MAX_LLM_SEED = (1 << 63) - 1


def derive_llm_seed(base_seed: int | None, namespace: str) -> int | None:
    """Derive a stable signed-64-bit-compatible seed for one inference stage."""
    if base_seed is None:
        return None
    payload = f"assistant-rh-llm-seed-v1\0{base_seed}\0{namespace}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & MAX_LLM_SEED


class LLMClient:
    """Thin wrapper around the OpenAI Python SDK for any compatible API."""

    def __init__(
        self,
        provider: str = "albert",
        model: str = "albert-large-chat",
        temperature: float = 0.0,
        system_prompt: str | None = None,
        timeout: int = 120,
        base_url: str | None = None,
        api_key_env: str | None = None,
    ):
        from openai import OpenAI

        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.timeout = timeout

        defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])
        api_key = os.getenv(api_key_env or defaults["key_env"], "")
        resolved_url = base_url or (os.getenv(defaults.get("url_env", ""), "").rstrip("/") or None)

        kwargs: dict = {"api_key": api_key}
        if resolved_url:
            kwargs["base_url"] = resolved_url
        self._client = OpenAI(**kwargs)

    # -- Synchronous ----------------------------------------------------------

    def chat(self, prompt: str, system_prompt: str | None = None, *, seed: int | None = None) -> str:
        messages = self._build_messages(prompt, system_prompt)
        create_kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            timeout=self.timeout,
        )
        if seed is not None:
            create_kwargs["seed"] = seed
        resp = self._client.chat.completions.create(**create_kwargs)
        return (resp.choices[0].message.content or "").strip()

    # -- Streaming ------------------------------------------------------------

    def chat_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history: list[Dict[str, str]] | None = None,
        *,
        seed: int | None = None,
    ) -> Generator[str, None, None]:
        messages = self._build_messages(prompt, system_prompt, history)
        create_kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            stream=True,
            timeout=self.timeout,
        )
        if seed is not None:
            create_kwargs["seed"] = seed
        stream = self._client.chat.completions.create(**create_kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                yield chunk.choices[0].delta.content

    # -- Helpers --------------------------------------------------------------

    def _build_messages(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history: list[Dict[str, str]] | None = None,
    ) -> list[dict]:
        sp = system_prompt if system_prompt is not None else self.system_prompt
        msgs: list[dict] = []
        if sp:
            msgs.append({"role": "system", "content": sp})
        for msg in history or []:
            msgs.append({"role": msg["role"], "content": msg["content"]})
        msgs.append({"role": "user", "content": prompt})
        return msgs


class FallbackLLMClient:
    """
    LLM client with automatic fallback.

    If the primary provider fails (before any token is streamed), the request
    is retried on the secondary provider.  Mid-stream failures cannot be
    recovered and an error marker is yielded instead.

    Attributes:
        last_provider_used: which provider served the last request.
        fallback_count: how many times the fallback was triggered.
    """

    def __init__(
        self,
        primary_provider: str = "albert",
        primary_model: str = "albert-large-chat",
        fallback_provider: str = "scaleway",
        fallback_model: str = "llama-3.1-70b-instruct",
        temperature: float = 0.0,
        system_prompt: str | None = None,
    ):
        self.last_provider_used: Optional[str] = None
        self.fallback_count: int = 0

        self._primary = _safe_init(primary_provider, primary_model, temperature, system_prompt)
        self._fallback = _safe_init(fallback_provider, fallback_model, temperature, system_prompt) if fallback_provider else None
        self._primary_name = primary_provider
        self._fallback_name = fallback_provider

    # -- Synchronous ----------------------------------------------------------

    def chat(self, prompt: str, system_prompt: str | None = None, *, seed: int | None = None) -> str:
        if self._primary:
            try:
                result = self._primary.chat(prompt, system_prompt, seed=seed)
                self.last_provider_used = self._primary_name
                return result
            except Exception as exc:
                logger.warning("Primary LLM (%s) failed: %s", self._primary_name, exc)

        if self._fallback:
            try:
                result = self._fallback.chat(prompt, system_prompt, seed=seed)
                self.last_provider_used = self._fallback_name
                self.fallback_count += 1
                return result
            except Exception as exc:
                logger.error("Fallback LLM (%s) also failed: %s", self._fallback_name, exc)

        raise RuntimeError("All LLM providers failed")

    # -- Streaming ------------------------------------------------------------

    def chat_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history: list[Dict[str, str]] | None = None,
        *,
        seed: int | None = None,
    ) -> Generator[str, None, None]:
        tokens_yielded = 0

        if self._primary:
            try:
                for tok in self._primary.chat_stream(prompt, system_prompt, history, seed=seed):
                    tokens_yielded += 1
                    yield tok
                self.last_provider_used = self._primary_name
                return
            except Exception as exc:
                if tokens_yielded > 0:
                    yield f"\n\n_Erreur de connexion ({self._primary_name}). Reponse partielle._"
                    return
                logger.warning("Primary LLM (%s) failed before streaming: %s", self._primary_name, exc)

        if self._fallback and tokens_yielded == 0:
            try:
                for tok in self._fallback.chat_stream(prompt, system_prompt, history, seed=seed):
                    yield tok
                self.last_provider_used = self._fallback_name
                self.fallback_count += 1
                return
            except Exception as exc:
                logger.error("Fallback LLM (%s) also failed: %s", self._fallback_name, exc)

        raise RuntimeError("All LLM providers failed")


def _safe_init(provider: str, model: str, temperature: float, system_prompt: str | None) -> Optional[LLMClient]:
    try:
        return LLMClient(provider=provider, model=model, temperature=temperature, system_prompt=system_prompt)
    except Exception as exc:
        logger.warning("Could not initialise LLM client (%s/%s): %s", provider, model, exc)
        return None


# Legacy alias – used by src/ui/chatbot_llm.py and other UI components.
ChatLLM = LLMClient
