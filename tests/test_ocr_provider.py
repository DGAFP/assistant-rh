"""Tests du fournisseur OCR Albert (utils/ocr.py) — HTTP mocké."""

from __future__ import annotations

import base64
import json
from typing import Any

import fitz
import pytest
from assistant_rh_data_engineering.utils import ocr as ocr_module
from assistant_rh_data_engineering.utils.ocr import (
    AlbertOcrProvider,
    OcrError,
    build_ocr_provider,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def make_provider(**kwargs: Any) -> AlbertOcrProvider:
    kwargs.setdefault("base_url", "https://albert.example/v1")
    kwargs.setdefault("api_key", "secret")
    return AlbertOcrProvider(**kwargs)


def make_pdf(page_count: int) -> bytes:
    document = fitz.open()
    try:
        for page_index in range(page_count):
            page = document.new_page()
            page.insert_text((72, 72), f"Page {page_index}")
        return document.tobytes()
    finally:
        document.close()


def test_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALBERT_API_KEY", raising=False)
    with pytest.raises(OcrError, match="ALBERT_API_KEY"):
        AlbertOcrProvider(base_url="https://albert.example/v1")


def test_ocr_pdf_posts_base64_document_and_joins_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        seen.update({"url": url, "headers": headers, "body": json, "timeout": timeout})
        return FakeResponse(
            {
                "model": "ocr-model-1",
                "pages": [
                    {"index": 1, "markdown": "Page deux", "images": []},
                    {"index": 0, "markdown": "# Page un", "images": []},
                    {"index": 2, "markdown": "  ", "images": []},
                ],
            }
        )

    monkeypatch.setattr(ocr_module.requests, "post", fake_post)

    result = make_provider(model="ocr-model-1").ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")

    assert seen["url"] == "https://albert.example/v1/ocr"
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["body"]["model"] == "ocr-model-1"
    document = seen["body"]["document"]
    assert document["type"] == "document_url"
    assert document["document_name"] == "MI-0001.pdf"
    encoded = document["document_url"].removeprefix("data:application/pdf;base64,")
    assert base64.b64decode(encoded) == b"%PDF-fake"

    # Pages triées par index, pages vides exclues du markdown concaténé.
    assert result.markdown == "# Page un\n\nPage deux"
    assert result.page_count == 3
    assert result.provider == "albert"
    assert result.version == "ocr-model-1"
    assert result.raw["model"] == "ocr-model-1"


def test_ocr_pdf_batches_large_documents_and_merges_images(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_pages: list[list[int]] = []

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        batch = json["pages"]
        requested_pages.append(batch)
        return FakeResponse(
            {
                "model": "ocr-model-1",
                "pages": [
                    {
                        "index": page_index,
                        "markdown": f"Page {page_index} ![img-0.jpeg](img-0.jpeg)",
                        "images": [{"id": "img-0.jpeg", "image_base64": "ZmFrZQ=="}],
                    }
                    for page_index in reversed(batch)
                ],
                "usage_info": {"pages_processed": len(batch), "doc_size_bytes": 1234},
            }
        )

    monkeypatch.setattr(ocr_module.requests, "post", fake_post)

    result = make_provider(model="ocr-model-1", include_images=True, max_pages_per_request=2).ocr_pdf(make_pdf(5))

    assert requested_pages == [[0, 1], [2, 3], [4]]
    assert [page["index"] for page in result.pages] == [0, 1, 2, 3, 4]
    assert [page["images"][0]["id"] for page in result.pages] == [
        "page-0000-img-0.jpeg",
        "page-0001-img-0.jpeg",
        "page-0002-img-0.jpeg",
        "page-0003-img-0.jpeg",
        "page-0004-img-0.jpeg",
    ]
    assert "![page-0000-img-0.jpeg](page-0000-img-0.jpeg)" in result.markdown
    assert "![page-0004-img-0.jpeg](page-0004-img-0.jpeg)" in result.markdown
    assert result.raw["usage_info"] == {"pages_processed": 5, "doc_size_bytes": 1234}


def test_ocr_pdf_keeps_single_request_at_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        seen["body"] = json
        return FakeResponse({"pages": [{"index": 0, "markdown": "texte"}]})

    monkeypatch.setattr(ocr_module.requests, "post", fake_post)

    make_provider(max_pages_per_request=2).ocr_pdf(make_pdf(2))

    assert "pages" not in seen["body"]


def test_provider_rejects_invalid_batch_size() -> None:
    with pytest.raises(OcrError, match="max_pages_per_request"):
        make_provider(max_pages_per_request=0)


def test_ocr_pdf_defaults_to_mistral_ocr_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # /ocr n'a pas de défaut serveur (404 sans modèle): le provider doit
    # toujours envoyer un modèle, mistral-ocr-2512 par défaut.
    monkeypatch.delenv("ALBERT_OCR_MODEL", raising=False)
    seen: dict[str, Any] = {}

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        seen["body"] = json
        return FakeResponse({"pages": [{"index": 0, "markdown": "texte", "images": []}]})

    monkeypatch.setattr(ocr_module.requests, "post", fake_post)

    result = make_provider().ocr_pdf(b"%PDF-fake")

    assert seen["body"]["model"] == "mistral-ocr-2512"
    assert result.version == "mistral-ocr-2512"


def test_ocr_pdf_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def busy_post(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse({"detail": "busy"}, status_code=503)

    monkeypatch.setattr(ocr_module.requests, "post", busy_post)
    monkeypatch.setattr(ocr_module.time, "sleep", sleeps.append)

    with pytest.raises(OcrError, match="HTTP 503"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")

    assert calls == 4
    assert sleeps == [10.0, 30.0, 60.0]


def test_ocr_pdf_recovers_from_transient_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            FakeResponse({"detail": "busy"}, status_code=503),
            FakeResponse({"pages": [{"index": 0, "markdown": "texte"}]}),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(ocr_module.requests, "post", lambda url, headers, json, timeout: next(responses))
    monkeypatch.setattr(ocr_module.time, "sleep", sleeps.append)

    result = make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")

    assert result.markdown == "texte"
    assert sleeps == [10.0]


def test_ocr_pdf_honors_numeric_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            FakeResponse({"detail": "rate limited"}, status_code=429, headers={"Retry-After": "2.5"}),
            FakeResponse({"pages": [{"index": 0, "markdown": "texte"}]}),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(ocr_module.requests, "post", lambda url, headers, json, timeout: next(responses))
    monkeypatch.setattr(ocr_module.time, "sleep", sleeps.append)

    make_provider().ocr_pdf(b"%PDF-fake")

    assert sleeps == [2.5]


def test_ocr_pdf_does_not_retry_non_transient_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def bad_request(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse({"detail": "invalid document"}, status_code=400)

    monkeypatch.setattr(ocr_module.requests, "post", bad_request)
    monkeypatch.setattr(ocr_module.time, "sleep", sleeps.append)

    with pytest.raises(OcrError, match="HTTP 400"):
        make_provider().ocr_pdf(b"%PDF-fake")

    assert calls == 1
    assert sleeps == []


def test_ocr_pdf_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def fail_post(url: str, headers: dict, json: dict, timeout: int) -> None:
        nonlocal calls
        calls += 1
        raise ocr_module.requests.RequestException("timeout")

    monkeypatch.setattr(ocr_module.requests, "post", fail_post)
    monkeypatch.setattr(ocr_module.time, "sleep", sleeps.append)

    with pytest.raises(OcrError, match="impossible"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")

    assert calls == 4
    assert sleeps == [10.0, 30.0, 60.0]


def test_ocr_pdf_reports_last_transport_error_after_http_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail_after_busy(url: str, headers: dict, json: dict, timeout: int) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse({"detail": "busy"}, status_code=503)
        raise ocr_module.requests.RequestException("connection reset")

    monkeypatch.setattr(ocr_module.requests, "post", fail_after_busy)
    monkeypatch.setattr(ocr_module.time, "sleep", lambda _: None)

    with pytest.raises(OcrError, match="connection reset"):
        make_provider().ocr_pdf(b"%PDF-fake")


def test_ocr_pdf_wraps_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class InvalidJsonResponse(FakeResponse):
        def json(self) -> dict[str, Any]:
            raise ValueError("not json")

    monkeypatch.setattr(ocr_module.requests, "post", lambda url, headers, json, timeout: InvalidJsonResponse({}))

    with pytest.raises(OcrError, match="JSON invalide"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")


def test_ocr_pdf_wraps_malformed_page_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocr_module.requests,
        "post",
        lambda url, headers, json, timeout: FakeResponse({"pages": [{"index": "not-an-int", "markdown": "texte"}]}),
    )

    with pytest.raises(OcrError, match="index de page invalide"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")


def test_ocr_pdf_raises_when_no_text_extracted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocr_module.requests,
        "post",
        lambda url, headers, json, timeout: FakeResponse({"pages": [{"index": 0, "markdown": "", "images": []}]}),
    )

    with pytest.raises(OcrError, match="sans texte exploitable"):
        make_provider().ocr_pdf(b"%PDF-fake")


def test_ocr_pdf_rejects_empty_pdf() -> None:
    with pytest.raises(OcrError, match="PDF vide"):
        make_provider().ocr_pdf(b"")


def test_version_is_path_safe() -> None:
    provider = make_provider(model="mistralai/Mistral-OCR 24.05")
    assert provider.version == "mistralai-Mistral-OCR-24.05"


def test_build_ocr_provider_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_API_KEY", "k")
    monkeypatch.delenv("OCR_PROVIDER", raising=False)

    provider = build_ocr_provider()
    assert isinstance(provider, AlbertOcrProvider)

    with pytest.raises(OcrError, match="Fournisseur OCR inconnu"):
        build_ocr_provider("lighton")
