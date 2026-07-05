"""Moteur de chunking QNA des corpus PDF ministériels (Phase D, #248).

Portage fidèle des notebooks d'ingestion legacy MATTE (scripts/extract_pdf.ipynb)
et MSO (scripts/extract_pdf_MSO.ipynb) — qui sont supprimés du repo au profit de
ce package. Les 2221 chunks legacy en base (959 MATTE + 1262 MSO) et le goldset
de conformité sont calés sur cette structure QNA (rôles Q_ONLY / QA_COMPOSITE /
A_ATOMIC / TABLE, questions inférées DANS le texte des chunks): la consigne de
la Phase D est de conserver cette logique, l'amélioration venant de l'amont
(OCR Albert au lieu de pdftotext/tesseract — 17/44 docs MATTE étaient à zéro
chunk faute d'extraction, audit #103).

Le moteur est paramétré par corpus (QnaEngineConfig): ordre des modes de
routage, format du texte des chunks (Q:/R: pour MATTE, Titre:/Section: pour
MSO), patterns de headings additionnels, taille du composite — pour rester
fidèle au comportement legacy de CHAQUE corpus tout en partageant le code.
"""

from .engine import QnaEngineConfig, SectionBlock, detect_document_mode, parse_document
from .gold import QnaGoldBuilder
from .silver import QnaSilverBuilder, flatten_ocr_to_text

__all__ = [
    "QnaEngineConfig",
    "QnaGoldBuilder",
    "QnaSilverBuilder",
    "SectionBlock",
    "detect_document_mode",
    "flatten_ocr_to_text",
    "parse_document",
]
