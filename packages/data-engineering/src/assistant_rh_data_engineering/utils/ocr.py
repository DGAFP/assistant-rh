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
_DEFAULT_MAX_PAGES_PER_REQUEST = 50


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


def _pdf_page_count(pdf_bytes: bytes) -> int | None:
    """Compte les pages localement; laisse Albert diagnostiquer un PDF invalide."""
    import fitz  # pymupdf

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except (RuntimeError, ValueError):
        return None
    try:
        return document.page_count
    finally:
        document.close()


def _namespace_page_images(page: dict[str, Any]) -> dict[str, Any]:
    """Rend les ids d'images uniques entre plusieurs réponses OCR fusionnées."""
    try:
        page_index = int(page.get("index") or 0)
    except (TypeError, ValueError):
        return dict(page)

    namespaced = dict(page)
    images: list[Any] = []
    replacements: dict[str, str] = {}
    for image in page.get("images") or []:
        if not isinstance(image, dict):
            images.append(image)
            continue
        image_copy = dict(image)
        image_id = str(image.get("id") or "").strip()
        if image_id:
            namespaced_id = f"page-{page_index:04d}-{image_id}"
            image_copy["id"] = namespaced_id
            replacements[image_id] = namespaced_id
        images.append(image_copy)

    markdown = str(page.get("markdown") or "")
    for image_id, namespaced_id in replacements.items():
        markdown = markdown.replace(image_id, namespaced_id)
    namespaced["markdown"] = markdown
    namespaced["images"] = images
    return namespaced


def _merge_batch_payloads(payloads: list[dict[str, Any]], document_name: str) -> dict[str, Any]:
    """Fusionne les lots Albert dans le contrat d'une réponse OCR unique."""
    if not payloads:
        raise OcrError(f"Aucune réponse OCR pour {document_name}")

    merged = dict(payloads[0])
    pages: list[dict[str, Any]] = []
    pages_processed = 0
    doc_size_bytes = 0
    for payload in payloads:
        raw_pages = payload.get("pages") or []
        if not isinstance(raw_pages, list):
            raise OcrError(f"Réponse OCR inattendue pour {document_name}: pages doit être une liste")
        for page in raw_pages:
            if not isinstance(page, dict):
                raise OcrError(f"Réponse OCR inattendue pour {document_name}: page doit être un objet")
            pages.append(_namespace_page_images(page))

        usage_info = payload.get("usage_info")
        if isinstance(usage_info, dict):
            try:
                pages_processed += int(usage_info.get("pages_processed") or 0)
                doc_size_bytes = max(doc_size_bytes, int(usage_info.get("doc_size_bytes") or 0))
            except (TypeError, ValueError):
                pass

    merged["pages"] = pages
    if pages_processed or doc_size_bytes:
        usage_info = dict(merged.get("usage_info") or {})
        if pages_processed:
            usage_info["pages_processed"] = pages_processed
        if doc_size_bytes:
            usage_info["doc_size_bytes"] = doc_size_bytes
        merged["usage_info"] = usage_info
    return merged


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
        max_pages_per_request: int = _DEFAULT_MAX_PAGES_PER_REQUEST,
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
        if max_pages_per_request < 1:
            raise OcrError("max_pages_per_request doit être supérieur ou égal à 1")
        self.max_pages_per_request = max_pages_per_request

    def _post_ocr(self, body: dict[str, Any], document_name: str, request_label: str) -> dict[str, Any]:
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
                    request_label,
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
                request_label,
                response.status_code,
                attempt + 1,
                max_attempts,
                delay,
            )
            time.sleep(delay)

        if response is None:
            raise OcrError(f"POST {url} ({request_label}, modèle {self.model}) impossible: {last_transport_error}") from last_transport_error
        if response.status_code >= 400:
            raise OcrError(f"POST {url} ({request_label}) -> HTTP {response.status_code}: {response.text[:500]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OcrError(f"Réponse OCR JSON invalide pour {document_name} (modèle {self.model})") from exc
        if not isinstance(payload, dict):
            raise OcrError(f"Réponse OCR inattendue pour {document_name} (modèle {self.model}): objet JSON attendu")
        return payload

    def ocr_pdf(self, pdf_bytes: bytes, document_name: str = "document.pdf") -> OcrResult:
        if not pdf_bytes:
            raise OcrError(f"PDF vide: {document_name}")

        document_url = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        body: dict[str, Any] = {
            "document": {
                "type": "document_url",
                "document_url": document_url,
                "document_name": document_name,
            },
            "model": self.model,
        }
        if self.include_images:
            body["include_image_base64"] = True

        page_count = _pdf_page_count(pdf_bytes)
        if page_count is not None and page_count > self.max_pages_per_request:
            batch_payloads: list[dict[str, Any]] = []
            logger.info(
                "OCR Albert par lots pour %s: %d pages, lots de %d",
                document_name,
                page_count,
                self.max_pages_per_request,
            )
            for start in range(0, page_count, self.max_pages_per_request):
                stop = min(start + self.max_pages_per_request, page_count)
                batch_body = {**body, "pages": list(range(start, stop))}
                batch_payloads.append(self._post_ocr(batch_body, document_name, f"{document_name}, pages {start}-{stop - 1}"))
            payload = _merge_batch_payloads(batch_payloads, document_name)
        else:
            payload = self._post_ocr(body, document_name, document_name)

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
