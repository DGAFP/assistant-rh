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

# Version de la LOGIQUE page-vision (détecteur is_risk_page + garde-fou
# is_faithful_reconstruction + politique de cap). Elle entre dans la clé de
# cache bronze (revue #320 M4b) : changer l'heuristique de détection ou le
# garde-fou change l'ensemble des pages reconstruites -> le cache doit être
# invalidé. À incrémenter à chaque évolution de ces règles.
PAGE_VISION_LOGIC_VERSION = "pvlogic2"

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

    def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
        """Reconstruit une page rendue: (markdown, tronquée).

        ``tronquée`` = la génération a atteint ``max_tokens`` (finish_reason
        "length") : la reconstruction est incomplète (ex. tableau coupé) et ne
        doit PAS remplacer l'OCR (revue #319 H2)."""
        data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 3000,
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
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PageVisionError(f"Réponse vision inattendue (modèle {self.model})") from exc
        truncated = str(choice.get("finish_reason") or "").lower() == "length"
        return _strip_markdown_fence(str(content or "")).strip(), truncated


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    """Certains VLM enveloppent la page dans un fence ```markdown …``` — on le
    retire pour que la reconstruction s'insère comme du markdown natif."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text


_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÿ]{3,}")


def _token_list(text: str) -> list[str]:
    """Liste (avec répétitions) des tokens significatifs — l'ordre/les doublons
    comptent pour la borne de LONGUEUR ; le vocabulaire (set) pour le rappel."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def is_faithful_reconstruction(
    reconstruction: str,
    ocr_markdown: str,
    *,
    min_ocr_tokens: int = 8,
    min_overlap: float = 0.5,
    max_growth: float = 3.0,
) -> bool:
    """Garde-fou anti-hallucination (revue #319/#320). La reconstruction remplace
    TOUT le markdown de la page ; elle doit :
    - **préserver** l'essentiel du VOCABULAIRE OCR (rappel des tokens uniques
      >= min_overlap) — sinon le VLM a halluciné une autre page / dérivé ;
    - ne pas **rallonger** massivement : sa LONGUEUR en tokens (occurrences, pas
      vocabulaire) est bornée à ``max_growth`` × celle de l'OCR. Compter les
      occurrences et non le set attrape une règle inventée répétée N fois, qui
      n'ajoute presque pas de vocabulaire (revue #320 finding 1).

    Une page OCR trop maigre (< min_ocr_tokens vocabulaire) est **non
    vérifiable** : on garde l'OCR plutôt qu'une sortie non contrôlable.

    N.B. ne détecte PAS une inversion fine (flèche CONTRAT lue AVENANT) : les
    tokens de gauche restent présents. Filet contre le faux contenu grossier,
    pas une vérification sémantique."""
    ocr_list = _token_list(_clean_page_text(ocr_markdown))
    ocr_vocab = set(ocr_list)
    if len(ocr_vocab) < min_ocr_tokens:
        return False  # non vérifiable -> ne pas remplacer l'OCR
    recon_list = _token_list(reconstruction)
    if not recon_list:
        return False
    overlap = len(ocr_vocab & set(recon_list)) / len(ocr_vocab)
    if overlap < min_overlap:
        return False  # rappel OCR insuffisant (mauvaise page / dérive)
    if len(recon_list) > len(ocr_list) * max_growth:
        return False  # trop rallongé (contenu inventé, même répété)
    return True


def reconstruct_pages(
    pdf_bytes: bytes,
    pages: list[dict[str, Any]],
    reconstructor: AlbertPageVisionReconstructor,
    *,
    positions: list[int] | None = None,
    guard_pages: list[dict[str, Any]] | None = None,
    max_pages: int = 60,
    max_workers: int = MAX_PAGE_VISION_WORKERS,
) -> tuple[dict[int, str], list[int]]:
    """Reconstruit les pages à risque d'un document: ({position: markdown}, [échecs]).

    ``guard_pages`` (défaut: ``pages``) porte l'OCR de RÉFÉRENCE pour le garde-fou
    de fidélité : l'appelant fournit l'OCR BRUT (pré-annotation), pas l'OCR enrichi
    de descriptions d'images synthétiques, sinon un rappel calculé contre du texte
    synthétique rejetterait à tort des reconstructions fidèles (revue #320 finding 2).

    ``positions`` force la liste (sinon détection heuristique). Trois issues par
    page :
    - **ok** -> reconstruction retenue.
    - **rejet** (troncature max_tokens, ou recouvrement OCR insuffisant =
      hallucination probable, ou sortie vide) -> la page reste en OCR, la
      position N'EST PAS dans ``failed`` : c'est déterministe, inutile de
      retenter en boucle, et le reste du document peut être mis en cache.
    - **panne** (rendu manquant, erreur/rate-limit VLM) -> position dans
      ``failed`` : transitoire, l'appelant ne met pas le lot en cache et
      retentera au prochain run.

    Ne fait donc jamais échouer l'ingestion (une page illisible reste en OCR)."""
    targets = positions if positions is not None else select_risk_positions(pages)
    if len(targets) > max_pages:
        # Perte silencieuse évitée (revue #319 M1): on trace les pages à risque
        # non reconstruites (conservées en OCR). Relever max_pages + --force-reocr
        # les reprend.
        dropped = len(targets) - max_pages
        print(f"[warn] {len(targets)} pages à risque > max_pages={max_pages} : {dropped} page(s) non reconstruite(s), conservées en OCR")
    targets = targets[:max_pages]
    if not targets:
        return {}, []

    guard = guard_pages if guard_pages is not None else pages
    pos_to_pdf = {pos: _pdf_index(pages[pos], pos) for pos in targets if 0 <= pos < len(pages)}
    images = render_pdf_pages(pdf_bytes, sorted(set(pos_to_pdf.values())), dpi=reconstructor.dpi)

    results: dict[int, str] = {}
    failed: list[int] = []

    def _one(pos: int) -> tuple[int, str | None, str]:
        image = images.get(pos_to_pdf.get(pos, -1))
        if image is None:
            return pos, None, "failed"  # rendu manquant (ex. index hors PDF) -> retry
        try:
            markdown, truncated = reconstructor.reconstruct(image)
        except PageVisionError as exc:
            print(f"[warn] reconstruction page {pos} échouée (VLM): {exc}")
            return pos, None, "failed"  # transitoire -> retry
        if not markdown:
            print(f"[warn] reconstruction page {pos} vide — page conservée en OCR")
            return pos, None, "rejected"
        if truncated:
            print(f"[warn] reconstruction page {pos} tronquée (max_tokens) — page conservée en OCR")
            return pos, None, "rejected"
        guard_md = str(guard[pos].get("markdown") or "") if 0 <= pos < len(guard) else ""
        if not is_faithful_reconstruction(markdown, guard_md):
            print(f"[warn] reconstruction page {pos} rejetée (recouvrement OCR insuffisant) — page conservée en OCR")
            return pos, None, "rejected"
        return pos, markdown, "ok"

    ordered = [pos for pos in targets if pos in pos_to_pdf]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ordered)))) as pool:
        for pos, markdown, status in pool.map(_one, ordered):
            if status == "ok" and markdown:
                results[pos] = markdown
            elif status == "failed":
                failed.append(pos)
            # rejet: page conservée en OCR, n'empêche pas la mise en cache du lot
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
