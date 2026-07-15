"""Re-passe vision pleine page: reconstruit les pages dont l'OCR a aplati la
structure (schémas à flèches, tableaux à deux colonnes, logigrammes) en rendant
la page entière en image et en la faisant reconstituer en Markdown par un VLM.

Constat (slide 57 de FORMATION_SGCD_mai_26.pdf, MASA, 2026-07-15): mistral-ocr
linéarise un tableau « type de changement → CONTRAT / AVENANT » relié par des
flèches en simple liste à puces de la colonne gauche, perdant la colonne droite
-> réponse RH fausse (« changement de catégorie » classé avenant au lieu de
contrat). L'annotation d'images (`image_annotation.py`) ne couvre PAS ce cas: le
schéma n'est pas un crop image (`page[].images`) mais du texte de page
linéarisé, jamais soumis au VLM. On rend donc la PAGE entière en image et on la
reconstruit — le VLM restitue les associations gauche→droite (PoC 2026-07-15:
les 9 lignes CONTRAT/AVENANT récupérées à l'identique).

Le rendu réutilise le même modèle vision Albert que l'annotation d'images
(`ALBERT_VISION_MODEL`, défaut openweight-medium) via /chat/completions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from .ocr import OcrResult, _sanitize_version

# Un échec/rate-limit VLM par page toléré (page conservée en OCR): la
# parallélisation reste modeste pour ne pas saturer l'API partagée, comme
# l'annotation d'images.
MAX_PAGE_VISION_WORKERS = 4

RECONSTRUCT_PROMPT = (
    "Tu reconstruis fidèlement le contenu d'une page d'un document de formation RH"
    " (souvent une diapositive) pour un moteur de recherche. Rends UNIQUEMENT le"
    " contenu de la page en Markdown français, sans préambule.\n\n"
    "RÈGLES CRITIQUES :\n"
    "- Restitue TOUT le contenu textuel de la page (ne résume pas, n'omets rien).\n"
    "- Préserve les tableaux avec leurs colonnes et lignes (format Markdown `| ... |`).\n"
    "- Préserve les FLÈCHES et les associations gauche→droite : si un élément de"
    " gauche pointe (flèche, colonne, accolade) vers une valeur de droite, rends-le"
    " explicitement, par exemple `Élément de gauche → Valeur de droite`, ou sous"
    " forme de tableau à deux colonnes.\n"
    "- Rends les logigrammes / schémas de décision comme des relations `source → cible`.\n"
    "- N'invente rien, ne commente pas l'apparence ni les couleurs.\n"
    "- Ignore les éléments purement décoratifs (logos, pieds de page répétés)."
)


class PageVisionError(RuntimeError):
    """Erreur d'appel au modèle vision pour la reconstruction de page."""


# --- Détection des pages à risque ------------------------------------------

_PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE:[^>]*-->", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)
_ARROW_RE = re.compile(r"→|-{1,2}>|⟶|➔|➜")
_OR_TOKEN_RE = re.compile(r"\bOU\b")


def _clean_page_text(page_markdown: str) -> str:
    return _PAGE_MARKER_RE.sub("", page_markdown or "").strip()


def is_risk_page(page_markdown: str, *, min_chars: int = 40, max_chars: int = 1400) -> bool:
    """Heuristique: la page porte-t-elle une structure (schéma/tableau/mapping)
    que l'OCR a pu aplatir ? Vrai sur les pages courtes-à-moyennes (slides) qui
    contiennent un tableau, une flèche, ou une liste sous un titre « X OU Y »
    (mapping à deux colonnes type CONTRAT/AVENANT). Les pages de prose dense
    (OCR fiable) et les pages quasi vides sont écartées."""
    text = _clean_page_text(page_markdown)
    n = len(text)
    if n < min_chars or n > max_chars:
        return False
    if "|" in text or _ARROW_RE.search(text):
        return True
    bullets = len(_BULLET_RE.findall(text))
    # Ligne en capitales contenant « OU » (ex. « MODIFICATION D'UN CONTRAT OU
    # AVENANT ») : signature d'un mapping gauche→droite que l'OCR a linéarisé.
    or_mapping = any(_OR_TOKEN_RE.search(line) and line.strip() == line.strip().upper() and len(line.strip()) > 8 for line in text.splitlines())
    return bullets >= 3 and or_mapping


def select_risk_positions(pages: list[dict[str, Any]]) -> list[int]:
    """Positions (dans la liste de pages OCR, triée par index) à reconstruire."""
    return [pos for pos, page in enumerate(pages) if is_risk_page(str(page.get("markdown") or ""))]


# --- Rendu PDF -> image -----------------------------------------------------


def _pdf_index(page: dict[str, Any], pos: int) -> int:
    try:
        return int(page.get("index"))
    except (TypeError, ValueError):
        return pos


def render_pdf_pages(pdf_bytes: bytes, pdf_indexes: list[int], *, dpi: int = 150) -> dict[int, bytes]:
    """Rend les pages PDF demandées en PNG ({index PDF: png}). Import paresseux
    de PyMuPDF pour ne pas l'imposer aux chemins qui n'ont pas besoin du rendu."""
    import fitz  # pymupdf

    out: dict[int, bytes] = {}
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i in pdf_indexes:
            if 0 <= i < doc.page_count:
                pix = doc.load_page(i).get_pixmap(dpi=dpi)
                out[i] = pix.tobytes("png")
    finally:
        doc.close()
    return out


# --- Reconstruction VLM -----------------------------------------------------


class AlbertPageVisionReconstructor:
    """Reconstruit une page rendue en image via /chat/completions (VLM Albert).

    name/version entrent dans la clé du cache bronze
    (page_vision/{name}/{version}/{sha256}.json): changer de modèle, de prompt
    OU de dpi invalide le cache (deux configurations produisent des
    reconstructions incomparables sous la même clé — même piège que le cache
    d'annotations d'images)."""

    name = "albert-page-vision"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        dpi: int = 150,
        timeout: int = 180,
    ):
        self.base_url = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.api_key = api_key or os.getenv("ALBERT_API_KEY", "")
        if not self.api_key:
            raise PageVisionError("ALBERT_API_KEY manquant pour la re-passe vision.")
        self.model = model or os.getenv("ALBERT_VISION_MODEL") or "openweight-medium"
        self.dpi = dpi
        prompt_hash = hashlib.sha1(RECONSTRUCT_PROMPT.encode("utf-8")).hexdigest()[:8]
        self.version = f"{_sanitize_version(self.model)}-p{prompt_hash}-d{dpi}"
        self.timeout = timeout
        self._session = requests.Session()

    def reconstruct(self, image_png: bytes) -> str:
        data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECONSTRUCT_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
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
            raise PageVisionError(f"POST {url} (modèle {self.model}) impossible: {exc}") from exc
        if response.status_code >= 400:
            raise PageVisionError(f"POST {url} -> HTTP {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PageVisionError(f"Réponse vision inattendue (modèle {self.model})") from exc
        return _strip_markdown_fence(str(content or "")).strip()


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    """Certains VLM enveloppent la page dans un fence ```markdown …``` — on le
    retire pour que la reconstruction s'insère comme du markdown natif."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text


def reconstruct_pages(
    pdf_bytes: bytes,
    pages: list[dict[str, Any]],
    reconstructor: AlbertPageVisionReconstructor,
    *,
    positions: list[int] | None = None,
    max_pages: int = 60,
    max_workers: int = MAX_PAGE_VISION_WORKERS,
) -> tuple[dict[int, str], list[int]]:
    """Reconstruit les pages à risque d'un document: ({position: markdown}, [échecs]).

    ``positions`` force la liste (sinon détection heuristique). Erreur par page
    tolérée (la page reste en OCR): une page illisible ne doit pas faire échouer
    l'ingestion. Les positions en échec sont retournées pour que l'appelant
    décide de la mise en cache (un lot partiel ne doit pas être gelé complet)."""
    targets = positions if positions is not None else select_risk_positions(pages)
    targets = targets[:max_pages]
    if not targets:
        return {}, []

    pos_to_pdf = {pos: _pdf_index(pages[pos], pos) for pos in targets if 0 <= pos < len(pages)}
    images = render_pdf_pages(pdf_bytes, sorted(set(pos_to_pdf.values())), dpi=reconstructor.dpi)

    results: dict[int, str] = {}
    failed: list[int] = []

    def _one(pos: int) -> tuple[int, str | None]:
        image = images.get(pos_to_pdf.get(pos, -1))
        if image is None:
            return pos, None
        try:
            markdown = reconstructor.reconstruct(image)
            return pos, (markdown or None)
        except PageVisionError as exc:
            print(f"[warn] reconstruction page {pos} échouée: {exc}")
            return pos, None

    ordered = [pos for pos in targets if pos in pos_to_pdf]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ordered)))) as pool:
        for pos, markdown in pool.map(_one, ordered):
            if markdown:
                results[pos] = markdown
            else:
                failed.append(pos)
    return results, failed


def apply_page_reconstructions(ocr_result: OcrResult, reconstructions: dict[int, str]) -> OcrResult:
    """Substitue le markdown des pages reconstruites et reconstruit le markdown
    agrégé (même concaténation que le provider OCR: pages triées, jointes par
    ``\\n\\n``). Marque les pages reconstruites (`page_vision=True`). N'écrase
    jamais ``raw`` (trace brute du provider)."""
    if not reconstructions:
        return ocr_result
    new_pages: list[dict[str, Any]] = []
    for pos, page in enumerate(ocr_result.pages):
        if pos in reconstructions:
            new_pages.append({**page, "markdown": reconstructions[pos], "page_vision": True})
        else:
            new_pages.append(page)
    markdown = "\n\n".join(str(page.get("markdown") or "").strip() for page in new_pages if str(page.get("markdown") or "").strip())
    return OcrResult(
        provider=ocr_result.provider,
        version=ocr_result.version,
        markdown=markdown,
        pages=new_pages,
        raw=ocr_result.raw,
    )
