"""Tests de la re-passe vision pleine page (utils/page_vision.py).

Constat fondateur (2026-07-15): l'OCR Mistral aplatit le schéma « type de
changement → CONTRAT / AVENANT » de la slide 57 (FORMATION_SGCD_mai_26.pdf) en
liste à puces, perdant la colonne droite. On vérifie ici la détection des pages
à risque, la substitution de leur markdown, et l'orchestration (rendu + VLM)
sans dépendre d'un vrai PDF ni de l'API Albert.
"""

from __future__ import annotations

from assistant_rh_data_engineering.utils import page_vision as pv
from assistant_rh_data_engineering.utils.ocr import OcrResult

# Le markdown OCR réel de la slide 57 (colonne droite CONTRAT/AVENANT perdue).
FLATTENED_SLIDE = (
    "3 MODIFICATION D'UN CONTRAT OU AVENANT\n\n"
    "- Changement d'affectation ou de fonction\n"
    "- Modification du statut (CDD / CDI) de l'agent contractuel\n"
    "- Changement de catégorie\n"
    "- Changement d'indice\n"
    "- Modification du fondement juridique\n"
)

PROSE_PAGE = (
    "## Préambule\n\n"
    "Cette formation s'inscrit dans une démarche de déconcentration de la gestion "
    "administrative des contrats à durée déterminée. " * 6
)


# --- Détection des pages à risque ------------------------------------------


def test_is_risk_page_detects_flattened_or_mapping() -> None:
    # Liste à puces sous un titre en capitales « ... OU ... » = mapping aplati.
    assert pv.is_risk_page(FLATTENED_SLIDE) is True


def test_is_risk_page_detects_table_and_arrows() -> None:
    assert pv.is_risk_page("| Col A | Col B |\n| --- | --- |\n| x | y |") is True
    assert pv.is_risk_page("Changement d'indice → AVENANT\nAutre ligne quelconque ici.") is True


def test_is_risk_page_skips_dense_prose_and_empty() -> None:
    assert pv.is_risk_page(PROSE_PAGE) is False  # trop long / prose
    assert pv.is_risk_page("") is False
    assert pv.is_risk_page("## Titre seul") is False  # pas de structure


def test_is_risk_page_ignores_page_markers() -> None:
    text = "<!-- PAGE: 57 -->\n\n" + FLATTENED_SLIDE
    assert pv.is_risk_page(text) is True


def test_select_risk_positions() -> None:
    pages = [
        {"index": 0, "markdown": PROSE_PAGE},
        {"index": 1, "markdown": FLATTENED_SLIDE},
        {"index": 2, "markdown": "## Titre seul"},
    ]
    assert pv.select_risk_positions(pages) == [1]


# --- Substitution -----------------------------------------------------------


def test_apply_page_reconstructions_substitutes_and_rebuilds_markdown() -> None:
    pages = [
        {"index": 0, "markdown": "Page zéro."},
        {"index": 1, "markdown": FLATTENED_SLIDE},
    ]
    ocr = OcrResult(provider="albert", version="v", markdown="Page zéro.\n\n" + FLATTENED_SLIDE, pages=pages)

    reconstructed = "- Changement de catégorie → CONTRAT\n- Changement d'indice → AVENANT"
    result = pv.apply_page_reconstructions(ocr, {1: reconstructed})

    assert result.pages[1]["markdown"] == reconstructed
    assert result.pages[1]["page_vision"] is True
    assert result.pages[0]["markdown"] == "Page zéro."  # page non ciblée intacte
    # Le markdown agrégé est reconstruit (même concaténation que le provider OCR).
    assert result.markdown == "Page zéro.\n\n" + reconstructed
    assert "→ CONTRAT" in result.markdown


def test_apply_page_reconstructions_noop_when_empty() -> None:
    ocr = OcrResult(provider="albert", version="v", markdown="x", pages=[{"index": 0, "markdown": "x"}])
    assert pv.apply_page_reconstructions(ocr, {}) is ocr


# --- Orchestration (rendu monkeypatché + VLM factice) -----------------------


class FakeReconstructor:
    name = "albert-page-vision"
    version = "fake-v-d150"
    dpi = 150

    def __init__(self) -> None:
        self.calls = 0

    def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
        self.calls += 1
        # Reconstruction fidèle: reprend les libellés OCR (colonne gauche) et
        # ajoute la colonne CONTRAT/AVENANT récupérée -> recouvrement élevé.
        return FAITHFUL_RECON, False


# Reconstruction plausible de FLATTENED_SLIDE (reprend ses libellés + la colonne
# droite) : passe le garde-fou de fidélité (recouvrement de tokens élevé).
FAITHFUL_RECON = (
    "- Changement d'affectation ou de fonction → CONTRAT\n"
    "- Modification du statut (CDD / CDI) de l'agent contractuel → CONTRAT\n"
    "- Changement de catégorie → CONTRAT\n"
    "- Changement d'indice → AVENANT\n"
    "- Modification du fondement juridique → CONTRAT\n"
)


def test_reconstruct_pages_renders_and_reconstructs_risk_pages(monkeypatch) -> None:
    pages = [
        {"index": 0, "markdown": PROSE_PAGE},
        {"index": 1, "markdown": FLATTENED_SLIDE},
    ]

    def fake_render(pdf_bytes: bytes, pdf_indexes: list[int], *, dpi: int = 150) -> dict[int, bytes]:
        assert pdf_indexes == [1]  # seule la page à risque est rendue
        return {i: f"png-{i}".encode() for i in pdf_indexes}

    monkeypatch.setattr(pv, "render_pdf_pages", fake_render)
    reconstructor = FakeReconstructor()

    reconstructions, failed = pv.reconstruct_pages(b"%PDF", pages, reconstructor)

    assert failed == []
    assert reconstructions == {1: FAITHFUL_RECON}
    assert reconstructor.calls == 1  # la page de prose n'est pas reconstruite


def test_reconstruct_pages_tolerates_vlm_failure(monkeypatch) -> None:
    pages = [{"index": 0, "markdown": FLATTENED_SLIDE}]
    monkeypatch.setattr(pv, "render_pdf_pages", lambda *a, **k: {0: b"png"})

    class FailingReconstructor(FakeReconstructor):
        def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
            raise pv.PageVisionError("VLM down")

    reconstructions, failed = pv.reconstruct_pages(b"%PDF", pages, FailingReconstructor())

    assert reconstructions == {}
    assert failed == [0]  # panne transitoire: position remontée (lot non caché, retry)


def test_reconstruct_pages_rejects_unfaithful_reconstruction(monkeypatch) -> None:
    # Le VLM hallucine une page sans rapport avec l'OCR: reconstruction REJETÉE
    # (page conservée en OCR), mais PAS dans `failed` (déterministe, pas de retry
    # en boucle) -> le lot reste cachable.
    pages = [{"index": 0, "markdown": FLATTENED_SLIDE}]
    monkeypatch.setattr(pv, "render_pdf_pages", lambda *a, **k: {0: b"png"})

    class HallucinatingReconstructor(FakeReconstructor):
        def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
            self.calls += 1
            return "Compte rendu de réunion budgétaire trimestrielle sans aucun rapport.", False

    reconstructions, failed = pv.reconstruct_pages(b"%PDF", pages, HallucinatingReconstructor())

    assert reconstructions == {}
    assert failed == []


def test_reconstruct_pages_rejects_truncated_reconstruction(monkeypatch) -> None:
    # Reconstruction fidèle mais tronquée (finish_reason=length): rejetée aussi
    # (mapping potentiellement incomplet) -> page conservée en OCR, pas de retry.
    pages = [{"index": 0, "markdown": FLATTENED_SLIDE}]
    monkeypatch.setattr(pv, "render_pdf_pages", lambda *a, **k: {0: b"png"})

    class TruncatedReconstructor(FakeReconstructor):
        def reconstruct(self, image_png: bytes) -> tuple[str, bool]:
            self.calls += 1
            return FAITHFUL_RECON, True

    reconstructions, failed = pv.reconstruct_pages(b"%PDF", pages, TruncatedReconstructor())

    assert reconstructions == {}
    assert failed == []


def test_is_faithful_reconstruction() -> None:
    # Recouvrement élevé (colonne gauche préservée), croissance raisonnable -> fidèle.
    assert pv.is_faithful_reconstruction(FAITHFUL_RECON, FLATTENED_SLIDE) is True
    # Contenu sans rapport -> non fidèle (rappel OCR insuffisant).
    assert pv.is_faithful_reconstruction("Texte totalement différent, réunion budgétaire.", FLATTENED_SLIDE) is False
    # Vide -> non fidèle.
    assert pv.is_faithful_reconstruction("", FLATTENED_SLIDE) is False
    # OCR trop maigre (< min_ocr_tokens) -> non vérifiable -> rejeté (garder OCR).
    assert pv.is_faithful_reconstruction("n'importe quoi", "## Titre") is False
    # Rappel OK mais contenu massivement AJOUTÉ (OCR + 100 tokens inventés
    # distincts) -> rejeté par la borne de croissance (revue #320 H2).
    padded = FLATTENED_SLIDE + " " + " ".join(f"motinvente{i}xyz" for i in range(100))
    assert pv.is_faithful_reconstruction(padded, FLATTENED_SLIDE) is False


def test_reconstructor_version_depends_on_prompt_and_dpi(monkeypatch) -> None:
    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    v_150 = pv.AlbertPageVisionReconstructor(dpi=150).version
    v_200 = pv.AlbertPageVisionReconstructor(dpi=200).version
    assert v_150 != v_200  # le dpi entre dans la clé de cache
    monkeypatch.setattr(pv, "RECONSTRUCT_PROMPT", pv.RECONSTRUCT_PROMPT + " Autre consigne.")
    v_prompt = pv.AlbertPageVisionReconstructor(dpi=150).version
    assert v_prompt != v_150  # le prompt aussi
