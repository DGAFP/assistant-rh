from __future__ import annotations

from types import SimpleNamespace

from assistant_rh_rag_pipeline.llm_client import MAX_LLM_SEED, LLMClient, derive_llm_seed


def test_derive_llm_seed_is_stable_namespaced_and_bounded() -> None:
    first = derive_llm_seed(42, "generator")

    assert first == derive_llm_seed(42, "generator")
    assert first != derive_llm_seed(42, "selector")
    assert first != derive_llm_seed(43, "generator")
    assert isinstance(first, int)
    assert 0 <= first <= MAX_LLM_SEED
    assert derive_llm_seed(None, "generator") is None


def test_llm_client_only_sends_seed_when_explicit(monkeypatch) -> None:
    calls: list[dict] = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    class _OpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr("openai.OpenAI", _OpenAI)
    client = LLMClient(provider="scaleway", model="model")

    assert client.chat("prompt", seed=123) == "ok"
    assert client.chat("prompt") == "ok"
    assert calls[0]["seed"] == 123
    assert "seed" not in calls[1]
