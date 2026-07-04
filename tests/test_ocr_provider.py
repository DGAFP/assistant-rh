"""Tests du fournisseur OCR Albert (utils/ocr.py) — HTTP mocké."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from assistant_rh_data_engineering.utils import ocr as ocr_module
from assistant_rh_data_engineering.utils.ocr import (
    AlbertOcrProvider,
    OcrError,
    build_ocr_provider,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def make_provider(**kwargs: Any) -> AlbertOcrProvider:
    kwargs.setdefault("base_url", "https://albert.example/v1")
    kwargs.setdefault("api_key", "secret")
    return AlbertOcrProvider(**kwargs)


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
    monkeypatch.setattr(
        ocr_module.requests,
        "post",
        lambda url, headers, json, timeout: FakeResponse({"detail": "busy"}, status_code=503),
    )

    with pytest.raises(OcrError, match="HTTP 503"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")


def test_ocr_pdf_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(url: str, headers: dict, json: dict, timeout: int) -> None:
        raise ocr_module.requests.RequestException("timeout")

    monkeypatch.setattr(ocr_module.requests, "post", fail_post)

    with pytest.raises(OcrError, match="impossible"):
        make_provider().ocr_pdf(b"%PDF-fake", document_name="MI-0001.pdf")


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
