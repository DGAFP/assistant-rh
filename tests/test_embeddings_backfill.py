from __future__ import annotations

from typing import Any

import pytest
import requests
from assistant_rh_data_engineering.jobs import embeddings_backfill


class DummyResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self.payload = payload or {"data": [{"embedding": [1.0, 0.0]}]}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)

    def json(self) -> dict[str, Any]:
        return self.payload


@pytest.fixture
def scaleway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://env.example.test/v1")
    monkeypatch.setenv("SCALEWAY_API_KEY", "env-key")


def test_scaleway_bge_client_resolves_base_url_from_explicit_then_env(scaleway_env: None) -> None:
    env_client = embeddings_backfill.ScalewayBgeClient(model_name="model")
    assert env_client.base_url == "https://env.example.test/v1"
    assert env_client.api_key == "env-key"

    explicit_client = embeddings_backfill.ScalewayBgeClient(
        model_name="model",
        base_url="https://explicit.example.test/v1",
    )
    assert explicit_client.base_url == "https://explicit.example.test/v1"


def test_scaleway_bge_client_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.setenv("SCALEWAY_API_KEY", "env-key")

    with pytest.raises(RuntimeError, match="SCALEWAY_BASE_URL"):
        embeddings_backfill.ScalewayBgeClient(model_name="model")


def test_scaleway_bge_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://env.example.test/v1")
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SCALEWAY_API_KEY"):
        embeddings_backfill.ScalewayBgeClient(model_name="model")


def test_scaleway_bge_client_does_not_validate_api_key_during_initialization(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_post(*args: Any, **kwargs: Any) -> DummyResponse:
        raise AssertionError("network validation should not run during initialization")

    session = requests.Session()
    monkeypatch.setattr(session, "post", fail_post)

    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    assert client.api_key == "env-key"


def test_embed_text_retries_and_raises_last_error(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)

    session = requests.Session()
    error = requests.ConnectionError("network down")

    def fail_post(*args: Any, **kwargs: Any) -> DummyResponse:
        raise error

    monkeypatch.setattr(session, "post", fail_post)
    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(requests.ConnectionError, match="network down"):
            client.embed_text("texte")

    assert "Erreur réseau embedding_bge_scw" in caplog.text


def test_embed_text_preserves_429_http_error_after_retries(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)

    session = requests.Session()
    post_calls = 0

    def rate_limited_post(*args: Any, **kwargs: Any) -> DummyResponse:
        nonlocal post_calls
        post_calls += 1
        return DummyResponse(status_code=429)

    monkeypatch.setattr(session, "post", rate_limited_post)
    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(requests.HTTPError) as exc_info:
            client.embed_text("texte")

    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 429
    assert post_calls == 6
    assert "Erreur HTTP 429" in caplog.text


def test_embed_text_retries_server_errors(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)

    session = requests.Session()
    responses = [DummyResponse(status_code=503), DummyResponse()]

    def flaky_post(*args: Any, **kwargs: Any) -> DummyResponse:
        return responses.pop(0)

    monkeypatch.setattr(session, "post", flaky_post)
    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    assert client.embed_text("texte") == [1.0, 0.0]


def test_embed_text_fails_fast_on_non_retryable_http_error(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    post_calls = 0

    def unauthorized_post(*args: Any, **kwargs: Any) -> DummyResponse:
        nonlocal post_calls
        post_calls += 1
        return DummyResponse(status_code=401)

    monkeypatch.setattr(session, "post", unauthorized_post)
    monkeypatch.setattr(
        embeddings_backfill.time,
        "sleep",
        lambda delay: pytest.fail("should not sleep on non-retryable error"),
    )
    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    with pytest.raises(requests.HTTPError) as exc_info:
        client.embed_text("texte")

    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 401
    assert post_calls == 1


def test_embed_text_fails_fast_on_invalid_payload(
    scaleway_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    post_calls = 0

    def invalid_payload_post(*args: Any, **kwargs: Any) -> DummyResponse:
        nonlocal post_calls
        post_calls += 1
        return DummyResponse(payload={"data": [{"embedding": "pas-une-liste"}]})

    monkeypatch.setattr(session, "post", invalid_payload_post)
    monkeypatch.setattr(
        embeddings_backfill.time,
        "sleep",
        lambda delay: pytest.fail("should not sleep on invalid payload"),
    )
    client = embeddings_backfill.ScalewayBgeClient(model_name="model", session=session)

    with pytest.raises(ValueError, match="embedding absent ou non-list"):
        client.embed_text("texte")

    assert post_calls == 1


def test_backfill_bge_scaleway_reuses_one_threadpool_without_shared_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": "1", "text": "a"},
        {"id": "2", "text": "b"},
        {"id": "3", "text": "c"},
    ]
    updates: list[list[dict[str, Any]]] = []
    executor_instances = 0
    client_kwargs: dict[str, Any] = {}

    class DummyClient:
        def __init__(self, **kwargs: Any):
            nonlocal client_kwargs
            client_kwargs = kwargs

        def embed_text(self, text: str) -> list[float]:
            return [float(ord(text) - ord("a") + 1), 0.0]

    class DummyExecutor:
        def __init__(self, max_workers: int):
            nonlocal executor_instances
            executor_instances += 1
            self.max_workers = max_workers

        def __enter__(self) -> "DummyExecutor":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def map(self, fn: Any, values: list[str]) -> list[list[float]]:
            return [fn(value) for value in values]

    monkeypatch.setattr(embeddings_backfill, "fetch_missing_rows", lambda *args: rows)
    monkeypatch.setattr(embeddings_backfill, "ScalewayBgeClient", DummyClient)
    monkeypatch.setattr(embeddings_backfill, "ThreadPoolExecutor", DummyExecutor)
    monkeypatch.setattr(
        embeddings_backfill,
        "update_embeddings",
        lambda conn, schema, table, id_column, embedding_column, prepared: updates.append(prepared) or len(prepared),
    )

    total = embeddings_backfill.backfill_bge_scaleway(
        conn=object(),
        schema="public",
        table_spec={"table": "rag_chunks", "id_column": "id", "text_column": "text"},
        embedding_column="embedding_bge_scw",
        model_name="model",
        base_url="https://example.test/v1",
        workers=2,
        batch_size=2,
        limit=None,
    )

    assert total == 3
    assert executor_instances == 1
    assert "session" not in client_kwargs
    assert [len(batch) for batch in updates] == [2, 1]


@pytest.fixture
def albert_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_BASE_URL", "https://albert.env.test/v1")
    monkeypatch.setenv("ALBERT_API_KEY", "albert-key")
    monkeypatch.delenv("ALBERT_EMBED_MODEL", raising=False)


def test_albert_embed_client_resolves_base_url_model_from_explicit_then_env(albert_env: None) -> None:
    env_client = embeddings_backfill.AlbertEmbedClient()
    assert env_client.base_url == "https://albert.env.test/v1"
    assert env_client.api_key == "albert-key"
    assert env_client.model_name == "openweight-embeddings"  # défaut aligné runtime

    explicit_client = embeddings_backfill.AlbertEmbedClient(model_name="m", base_url="https://explicit.test/v1")
    assert explicit_client.base_url == "https://explicit.test/v1"
    assert explicit_client.model_name == "m"


def test_albert_embed_client_defaults_base_url_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALBERT_BASE_URL", raising=False)
    monkeypatch.setenv("ALBERT_API_KEY", "albert-key")
    client = embeddings_backfill.AlbertEmbedClient()
    assert client.base_url == "https://albert.api.etalab.gouv.fr/v1"


def test_albert_embed_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_BASE_URL", "https://albert.env.test/v1")
    monkeypatch.delenv("ALBERT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ALBERT_API_KEY"):
        embeddings_backfill.AlbertEmbedClient()


def test_albert_embed_client_posts_model_and_retries_server_error(
    albert_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)
    session = requests.Session()
    captured: dict[str, Any] = {}
    responses = [DummyResponse(status_code=503), DummyResponse()]

    def flaky_post(url: str, **kwargs: Any) -> DummyResponse:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return responses.pop(0)

    monkeypatch.setattr(session, "post", flaky_post)
    client = embeddings_backfill.AlbertEmbedClient(session=session)

    assert client.embed_text("texte") == [1.0, 0.0]  # 503 puis 200 (retry via helper partagé)
    assert captured["url"] == "https://albert.env.test/v1/embeddings"
    assert captured["json"] == {"model": "openweight-embeddings", "input": "texte"}


def test_backfill_albert_uses_albert_client_and_one_threadpool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}, {"id": "3", "text": "c"}]
    updates: list[list[dict[str, Any]]] = []
    used_client: list[str] = []

    class DummyClient:
        def __init__(self, **kwargs: Any):
            used_client.append("albert")

        def embed_text(self, text: str) -> list[float]:
            return [float(ord(text) - ord("a") + 1), 0.0]

    class DummyExecutor:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self) -> DummyExecutor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def map(self, fn: Any, values: list[str]) -> list[list[float]]:
            return [fn(value) for value in values]

    monkeypatch.setattr(embeddings_backfill, "fetch_missing_rows", lambda *args: rows)
    monkeypatch.setattr(embeddings_backfill, "AlbertEmbedClient", DummyClient)
    monkeypatch.setattr(embeddings_backfill, "ThreadPoolExecutor", DummyExecutor)
    monkeypatch.setattr(
        embeddings_backfill,
        "update_embeddings",
        lambda conn, schema, table, id_column, embedding_column, prepared: updates.append(prepared) or len(prepared),
    )

    total = embeddings_backfill.backfill_albert(
        conn=object(),
        schema="public",
        table_spec={"table": "rag_chunks_dgafp", "id_column": "chunk_id", "text_column": "chunk_text"},
        embedding_column="embedding_m3",
        model_name="openweight-embeddings",
        base_url=None,
        workers=4,
        batch_size=2,
        limit=None,
    )

    assert total == 3
    assert used_client == ["albert"]
    assert [len(batch) for batch in updates] == [2, 1]
