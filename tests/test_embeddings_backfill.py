from __future__ import annotations

from pathlib import Path
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


def test_scaleway_bge_client_resolves_base_url_from_explicit_env_and_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SCALEWAY_BASE_URL=https://dotenv.example.test/v1\nSCALEWAY_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)

    dotenv_client = embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model")
    assert dotenv_client.base_url == "https://dotenv.example.test/v1"
    assert dotenv_client.api_key == "dotenv-key"

    monkeypatch.setenv("SCALEWAY_BASE_URL", "https://env.example.test/v1")
    monkeypatch.setenv("SCALEWAY_API_KEY", "env-key")
    env_client = embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model")
    assert env_client.base_url == "https://env.example.test/v1"
    assert env_client.api_key == "env-key"

    explicit_client = embeddings_backfill.ScalewayBgeClient(
        env_path=env_path,
        model_name="model",
        base_url="https://explicit.example.test/v1",
    )
    assert explicit_client.base_url == "https://explicit.example.test/v1"


def test_scaleway_bge_client_requires_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("SCALEWAY_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SCALEWAY_BASE_URL"):
        embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model")


def test_scaleway_bge_client_does_not_validate_api_key_during_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SCALEWAY_BASE_URL=https://dotenv.example.test/v1\nSCALEWAY_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)

    def fail_post(*args: Any, **kwargs: Any) -> DummyResponse:
        raise AssertionError("network validation should not run during initialization")

    session = requests.Session()
    monkeypatch.setattr(session, "post", fail_post)

    client = embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model", session=session)

    assert client.api_key == "dotenv-key"


def test_embed_text_retries_and_raises_last_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SCALEWAY_BASE_URL=https://dotenv.example.test/v1\nSCALEWAY_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)

    session = requests.Session()
    error = requests.ConnectionError("network down")

    def fail_post(*args: Any, **kwargs: Any) -> DummyResponse:
        raise error

    monkeypatch.setattr(session, "post", fail_post)
    client = embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model", session=session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(requests.ConnectionError, match="network down"):
            client.embed_text("texte")

    assert "Erreur embedding_bge_scw" in caplog.text


def test_embed_text_preserves_429_http_error_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SCALEWAY_BASE_URL=https://dotenv.example.test/v1\nSCALEWAY_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(embeddings_backfill.time, "sleep", lambda delay: None)

    session = requests.Session()

    def rate_limited_post(*args: Any, **kwargs: Any) -> DummyResponse:
        return DummyResponse(status_code=429)

    monkeypatch.setattr(session, "post", rate_limited_post)
    client = embeddings_backfill.ScalewayBgeClient(env_path=env_path, model_name="model", session=session)

    with caplog.at_level("DEBUG"):
        with pytest.raises(requests.HTTPError) as exc_info:
            client.embed_text("texte")

    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 429
    assert "Scaleway embeddings rate limit" in caplog.text


def test_backfill_bge_scaleway_reuses_one_threadpool_across_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": "1", "text": "a"},
        {"id": "2", "text": "b"},
        {"id": "3", "text": "c"},
    ]
    updates: list[list[dict[str, Any]]] = []
    executor_instances = 0
    session_closed = False

    class DummyClient:
        def __init__(self, **kwargs: Any):
            assert "session" in kwargs

        def embed_text(self, text: str) -> list[float]:
            return [float(ord(text) - ord("a") + 1), 0.0]

    class DummySession:
        def __enter__(self) -> "DummySession":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            nonlocal session_closed
            session_closed = True

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
    monkeypatch.setattr(embeddings_backfill.requests, "Session", DummySession)
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
        env_path=tmp_path / ".env",
        model_name="model",
        base_url="https://example.test/v1",
        workers=2,
        batch_size=2,
        limit=None,
    )

    assert total == 3
    assert executor_instances == 1
    assert session_closed is True
    assert [len(batch) for batch in updates] == [2, 1]
