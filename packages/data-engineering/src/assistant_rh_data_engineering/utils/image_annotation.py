"""Enrichissement des images OCR par un modèle vision (Albert).

Constat sur le lot MASA réel (2026-07-04): mistral-ocr extrait les images en
crops référencés `![img-N.jpeg](img-N.jpeg)` dans le markdown — copies d'écran
RenoiRH porteuses d'information procédurale, mais aussi photos décoratives
(branding ministériel) et pictogrammes. L'annotation native du contrat Mistral
OCR (`bbox_annotation_format`) est inutilisable sur Albert à ce jour: la
passerelle exige `json_schema.schema_definition`, le service interne l'interdit
et exige `json_schema.schema` — validations incompatibles en cascade (422 dans
les deux sens, smoke test 2026-07-04).

Voie retenue: `include_image_base64` à l'OCR (supporté), puis un modèle vision
Albert classe chaque crop (décoratif/informatif) et extrait l'information des
informatifs. Les descriptions remplacent les références d'images dans le
markdown AVANT sectionnement; les décoratifs sont retirés.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from .ocr import _sanitize_version

# Référence d'image markdown `![...](cible)`. Partagée avec le filtre de
# payload de masa/gold.py: les deux passes doivent voir les mêmes images.
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Un 429/panne Albert toléré par image serait gelé au cache si le lot partiel
# était persisté (voir masa/bronze.py): la parallélisation reste volontairement
# modeste pour ne pas provoquer de rate-limit sur l'API partagée.
MAX_ANNOTATION_WORKERS = 4

ANNOTATION_PROMPT = (
    "Tu enrichis un corpus documentaire RH pour un moteur de recherche. Analyse cette image extraite"
    " d'un document (support de formation ou circulaire d'un ministère).\n\n"
    'Réponds UNIQUEMENT en JSON: {"type_image": "decorative"|"informative", "description": "..."}\n\n'
    '- "decorative": photo d\'illustration, logo, pictogramme générique sans information métier -> description = "".\n'
    "- \"informative\": copie d'écran d'application, schéma de processus, tableau, graphique -> description ="
    " l'information contenue, en français: nom de l'application, chemin de menu, champs et valeurs visibles,"
    " étapes du processus. Concis (max 120 mots), factuel, sans commenter l'apparence."
)


class ImageAnnotationError(RuntimeError):
    """Erreur d'appel au modèle vision."""


class AlbertImageAnnotator:
    """Annotation d'un crop d'image via /chat/completions (modèle vision Albert).

    name/version entrent dans la clé du cache bronze des annotations
    (image_annotations/{name}/{version}/{sha256}.json): changer de modèle OU
    de prompt invalide le cache (le hash du prompt entre dans la version —
    même piège que include_images pour le cache OCR: deux prompts différents
    produisent des annotations incomparables sous la même clé).
    """

    name = "albert"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
    ):
        self.base_url = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.api_key = api_key or os.getenv("ALBERT_API_KEY", "")
        if not self.api_key:
            raise ImageAnnotationError("ALBERT_API_KEY manquant pour l'annotation d'images.")
        self.model = model or os.getenv("ALBERT_VISION_MODEL") or "openweight-medium"
        prompt_hash = hashlib.sha1(ANNOTATION_PROMPT.encode("utf-8")).hexdigest()[:8]
        self.version = f"{_sanitize_version(self.model)}-p{prompt_hash}"
        self.timeout = timeout
        # Session partagée: réutilise les connexions TLS entre crops (le pool
        # urllib3 sous-jacent est thread-safe pour des POST simples).
        self._session = requests.Session()

    def annotate(self, image_data_url: str) -> dict[str, str]:
        """Retourne {"type_image": "decorative"|"informative", "description": str}."""
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ANNOTATION_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
        }
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ImageAnnotationError(f"POST {url} (modèle {self.model}) impossible: {exc}") from exc
        if response.status_code >= 400:
            raise ImageAnnotationError(f"POST {url} -> HTTP {response.status_code}: {response.text[:300]}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ImageAnnotationError(f"Réponse vision inattendue (modèle {self.model})") from exc
        return _parse_annotation(str(content))


def _parse_annotation(content: str) -> dict[str, str]:
    """Parse tolérant de la réponse du VLM (fences markdown, champs manquants)."""
    text = content.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImageAnnotationError(f"Annotation non-JSON: {content[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ImageAnnotationError(f"Annotation non-objet: {content[:200]!r}")
    type_image = str(data.get("type_image") or "").strip().lower()
    if type_image not in {"decorative", "informative"}:
        raise ImageAnnotationError(f"type_image invalide: {content[:200]!r}")
    return {"type_image": type_image, "description": str(data.get("description") or "").strip()}


def iter_ocr_images(pages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Liste (image_id, data_url) des crops présents dans une réponse OCR."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in pages:
        for image in page.get("images") or []:
            image_id = str(image.get("id") or "").strip()
            b64 = image.get("image_base64")
            if not image_id or not b64 or image_id in seen:
                continue
            seen.add(image_id)
            data_url = b64 if str(b64).startswith("data:") else f"data:image/jpeg;base64,{b64}"
            found.append((image_id, str(data_url)))
    return found


def annotate_ocr_images(
    pages: list[dict[str, Any]],
    annotator: AlbertImageAnnotator,
    *,
    max_images: int = 150,
    max_workers: int = MAX_ANNOTATION_WORKERS,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Annote les crops d'une réponse OCR: ({image_id: annotation}, [échecs]).

    Erreur par image tolérée (l'image reste non annotée, sa référence markdown
    est conservée telle quelle): une image illisible ne doit pas faire échouer
    l'ingestion du document. Les ids en échec sont retournés pour que
    l'appelant décide de la mise en cache — un lot partiel ne doit pas être
    gelé comme s'il était complet. `max_images` borne le coût VLM par
    document; les appels sont indépendants et parallélisés modérément.
    """
    items = iter_ocr_images(pages)[:max_images]
    annotations: dict[str, dict[str, str]] = {}
    failed: list[str] = []
    if not items:
        return annotations, failed

    def _one(item: tuple[str, str]) -> tuple[str, dict[str, str] | None]:
        image_id, data_url = item
        try:
            return image_id, annotator.annotate(data_url)
        except ImageAnnotationError as exc:
            print(f"[warn] annotation image {image_id} échouée: {exc}")
            return image_id, None

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as pool:
        for image_id, annotation in pool.map(_one, items):
            if annotation is None:
                failed.append(image_id)
            else:
                annotations[image_id] = annotation
    return annotations, failed


def apply_image_annotations(markdown: str, annotations: dict[str, dict[str, str]]) -> str:
    """Remplace les références d'images du markdown par leur contenu utile.

    - informative avec description => paragraphe `[Illustration — ...]`,
      retrievable et sectionnable comme du texte.
    - decorative => référence retirée (bruit).
    - non annotée (échec VLM, hors budget max_images) ou informative SANS
      description (réponse VLM incomplète) => référence conservée telle
      quelle: on ne supprime jamais une image potentiellement porteuse
      d'information sans description de remplacement.
    """

    def _replace(match: re.Match[str]) -> str:
        image_id = match.group(1).strip()
        annotation = annotations.get(image_id)
        if annotation is None:
            return match.group(0)
        if annotation["type_image"] == "decorative":
            return ""
        if annotation["description"]:
            return f"[Illustration — {annotation['description']}]"
        return match.group(0)

    return IMAGE_REF_RE.sub(_replace, markdown)
