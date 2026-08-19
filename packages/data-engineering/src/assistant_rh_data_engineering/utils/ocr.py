from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

_OCR_RETRY_BACKOFF_SECONDS = (10.0, 30.0, 60.0)


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


def _retry_delay(response: requests.Response, fallback_seconds: float) -> float:
    """Utilise Retry-After quand il contient un délai numérique valide."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return fallback_seconds


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
        include_images: bool = False,
    ):
        self.base_url = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.api_key = api_key or os.getenv("ALBERT_API_KEY", "")
        if not self.api_key:
            raise OcrError("ALBERT_API_KEY manquant pour l'OCR Albert.")
        # L'endpoint /ocr n'a pas de modèle par défaut côté serveur (404
        # "Model not found" sans modèle; smoke test 2026-07-03). Seuls les
        # modèles de type image-to-text sont acceptés (LightOnOCR est
        # image-text-to-text => 422, à intégrer via un provider dédié).
        self.model = model or os.getenv("ALBERT_OCR_MODEL") or "mistral-ocr-2512"
        # include_images change le contenu de la réponse (crops base64 dans
        # pages[].images): la version — donc la clé du cache bronze — doit en
        # dépendre, sinon un cache rempli sans images empêcherait à jamais
        # l'enrichissement des documents déjà OCRisés.
        self.include_images = include_images
        self.version = _sanitize_version(f"{self.model}-img" if include_images else self.model)
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
        body["model"] = self.model
        if self.include_images:
            body["include_image_base64"] = True

        url = f"{self.base_url}/ocr"
        response: requests.Response | None = None
        last_transport_error: requests.RequestException | None = None
        max_attempts = len(_OCR_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(max_attempts):
            try:
                response = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout,
                )
                last_transport_error = None
            except requests.RequestException as exc:
                response = None
                last_transport_error = exc
                if attempt == max_attempts - 1:
                    break
                delay = _OCR_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "OCR Albert indisponible pour %s (erreur réseau, tentative %d/%d); nouvel essai dans %.0fs",
                    document_name,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            if response.status_code != 429 and not 500 <= response.status_code < 600:
                break
            if attempt == max_attempts - 1:
                break
            delay = _retry_delay(response, _OCR_RETRY_BACKOFF_SECONDS[attempt])
            logger.warning(
                "OCR Albert indisponible pour %s (HTTP %d, tentative %d/%d); nouvel essai dans %.0fs",
                document_name,
                response.status_code,
                attempt + 1,
                max_attempts,
                delay,
            )
            time.sleep(delay)

        if response is None:
            raise OcrError(f"POST {url} ({document_name}, modèle {self.model}) impossible: {last_transport_error}") from last_transport_error
        if response.status_code >= 400:
            raise OcrError(f"POST {url} ({document_name}) -> HTTP {response.status_code}: {response.text[:500]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OcrError(f"Réponse OCR JSON invalide pour {document_name} (modèle {self.model})") from exc
        if not isinstance(payload, dict):
            raise OcrError(f"Réponse OCR inattendue pour {document_name} (modèle {self.model}): objet JSON attendu")

        raw_pages = payload.get("pages") or []
        if not isinstance(raw_pages, list):
            raise OcrError(f"Réponse OCR inattendue pour {document_name} (modèle {self.model}): pages doit être une liste")

        try:
            pages = sorted(raw_pages, key=lambda page: int(page.get("index") or 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise OcrError(f"Réponse OCR invalide pour {document_name} (modèle {self.model}): index de page invalide") from exc
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


def build_ocr_provider(provider_name: str | None = None, *, include_images: bool = False) -> OcrProvider:
    """Fabrique le fournisseur OCR (env OCR_PROVIDER, défaut: albert).

    LightOn/Mistral s'ajouteront ici comme nouvelles classes sans toucher aux
    pipelines (décision 2026-07-03: interface stable, fournisseur swappable).
    """
    resolved = (provider_name or os.getenv("OCR_PROVIDER") or "albert").strip().lower()
    if resolved == "albert":
        return AlbertOcrProvider(include_images=include_images)
    raise OcrError(f"Fournisseur OCR inconnu: {resolved!r} (disponibles: albert)")
