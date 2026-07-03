from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests


class OcrError(RuntimeError):
    """Erreur d'appel au service OCR."""


@dataclass(frozen=True)
class OcrResult:
    """Sortie OCR normalisée, indépendante du fournisseur.

    markdown est la concaténation des pages (ordre des index); raw est la
    réponse brute du fournisseur, archivée telle quelle dans le cache bronze
    pour permettre re-parsing et comparaison de fournisseurs sans re-payer.
    """

    provider: str
    version: str
    markdown: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)


class OcrProvider:
    """Interface des fournisseurs OCR (même motif que BaseBatchEmbedder).

    version identifie le modèle/configuration: elle entre dans la clé du
    cache bronze (provider/version/sha256), donc elle doit être connue avant
    l'appel et stable pour une configuration donnée.
    """

    name: str
    version: str

    def ocr_pdf(self, pdf_bytes: bytes, document_name: str = "document.pdf") -> OcrResult:
        raise NotImplementedError


def _sanitize_version(value: str) -> str:
    # La version sert de segment de chemin S3: on neutralise séparateurs et espaces.
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()) or "default"


class AlbertOcrProvider(OcrProvider):
    """OCR via l'endpoint /v1/ocr de l'API Albert (contrat type Mistral OCR).

    Requête: {"model": ..., "document": {"type": "document_url",
    "document_url": "data:application/pdf;base64,..."}}.
    Réponse: {"pages": [{"index": int, "markdown": str, ...}], "model": ...}.
    """

    name = "albert"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 300,
    ):
        self.base_url = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.api_key = api_key or os.getenv("ALBERT_API_KEY", "")
        if not self.api_key:
            raise OcrError("ALBERT_API_KEY manquant pour l'OCR Albert.")
        # Modèle optionnel: l'API applique son défaut si absent. La version de
        # cache reste déterministe car dérivée de la configuration, pas de la
        # réponse.
        self.model = model or os.getenv("ALBERT_OCR_MODEL") or None
        self.version = _sanitize_version(self.model or "default")
        self.timeout = timeout

    def ocr_pdf(self, pdf_bytes: bytes, document_name: str = "document.pdf") -> OcrResult:
        if not pdf_bytes:
            raise OcrError(f"PDF vide: {document_name}")

        document_url = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        body: dict[str, Any] = {
            "document": {
                "type": "document_url",
                "document_url": document_url,
                "document_name": document_name,
            }
        }
        if self.model:
            body["model"] = self.model

        url = f"{self.base_url}/ocr"
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise OcrError(f"POST {url} ({document_name}) -> HTTP {response.status_code}: {response.text[:500]}")

        payload = response.json()
        pages = sorted(payload.get("pages") or [], key=lambda page: int(page.get("index") or 0))
        markdown = "\n\n".join(str(page.get("markdown") or "").strip() for page in pages if (page.get("markdown") or "").strip())
        if not markdown:
            raise OcrError(f"OCR sans texte exploitable pour {document_name} ({len(pages)} pages)")

        return OcrResult(
            provider=self.name,
            version=self.version,
            markdown=markdown,
            pages=pages,
            raw=payload,
        )


def build_ocr_provider(provider_name: str | None = None) -> OcrProvider:
    """Fabrique le fournisseur OCR (env OCR_PROVIDER, défaut: albert).

    LightOn/Mistral s'ajouteront ici comme nouvelles classes sans toucher aux
    pipelines (décision 2026-07-03: interface stable, fournisseur swappable).
    """
    resolved = (provider_name or os.getenv("OCR_PROVIDER") or "albert").strip().lower()
    if resolved == "albert":
        return AlbertOcrProvider()
    raise OcrError(f"Fournisseur OCR inconnu: {resolved!r} (disponibles: albert)")
