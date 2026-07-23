"""Silver QNA: markdown OCR -> texte plat -> SectionBlocks -> rag_documents/rag_sections.

Les parseurs legacy consomment du texte plat ligne à ligne (sortie pdftotext
avec marqueurs [PAGE n]); mistral-ocr sort du markdown. flatten_ocr_to_text
fait l'aplatissement à drift minimal: marqueurs de page conservés au format
legacy, tableaux externalisés (page["tables"]) réinjectés puis dé-pipés,
refs d'images non annotées retirées.

Divergences assumées vs l'aplatissement legacy (issue #302, docs cassés):
- Les headings markdown de l'OCR ne sont plus détruits mais traduits en
  marqueurs de structure `(Titre)`/`(Intertitre)` que detect_heading comprend
  déjà — le mode guide suit alors la vraie structure du document au lieu de
  re-deviner des headings par heuristiques (qui promouvaient les en-têtes de
  courrier « ET DES SOLIDARITÉS » et des lignes de tableaux en titres de
  sections). Les autres parseurs retirent le marqueur ligne à ligne
  (strip_structure_marker) et voient le même texte qu'avant.
- Les en-têtes/pieds de page répétés (letterhead « MINISTÈRES SOCIAUX /
  Liberté / Égalité / Fraternité », folios « Bulletin officiel … Page n »)
  sont retirés avant aplatissement — même principe que le
  _page_edge_boilerplate du silver heading-based, fenêtre élargie aux pages
  courtes (slides).
- Les tableaux-matrices à cases cochées (en-tête sur deux lignes + cellules
  ☑/X) sont sérialisés ligne à ligne avec leurs noms de colonnes: le dé-pipage
  legacy perdait la colonne porteuse de la réponse (« produit par : DRHM ou
  SD ? »).

Divergences connues (arbitrage au goldset, règle de la Phase D):
- Documents « process » type slides/logigramme (ex: « Je recrute un contractuel
  en AC »): l'OCR linéarise les slides autrement que pdftotext, le parseur
  process (porté à l'identique) en tire moins de sections que le legacy
  (3-12 vs 28-34 chunks). Non bloquant (jamais zéro chunk, le fallback couvre),
  contenu intrinsèquement visuel; les descriptions VLM d'images ([Illustration
  — …]) compensent en partie. À ré-arbitrer si le goldset MSO régresse.
"""

from __future__ import annotations

import re
from collections import Counter
from math import ceil
from typing import Any

from ...service_public.section_splitter import count_tokens
from ...utils.helpers import sha256_text, stable_uuid_from_parts, utc_now_iso
from ...utils.image_annotation import IMAGE_REF_RE
from ...utils.ocr import OcrResult
from ...utils.silver import SilverBundle, SilverRepository
from ..bronze import BronzeAsset
from ..identity import MinistryIdentity
from .engine import QnaEngineConfig, SectionBlock, normalize_text, parse_document

__all__ = ["QnaSilverBuilder", "flatten_ocr_to_text"]

_H1_MARKER_RE = re.compile(r"^#\s+")
_HEADING_MARKER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TABLE_SEPARATOR_ROW_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")

# Cellule « case cochée » d'un tableau-matrice (croix ou coche).
_MATRIX_MARK_RE = re.compile(r"^[☑✓✔Xx×]$")

# Mapping mode de parsing -> doc_type (repris du notebook MSO).
_MODE_DOC_TYPES = {
    "table_matrix": "Tableau RH",
    "process": "Processus RH",
    "faq": "FAQ RH",
    "qna_markers": "FAQ RH",
}


# Référence d'une table externalisée par mistral-ocr: `[tbl-0.md](tbl-0.md)`.
# Le contenu réel (markdown de tableau) vit dans page["tables"], pas dans le
# markdown de page — il faut l'inliner avant l'aplatissement, sinon les
# parseurs table_matrix/process ne voient rien (régression du rebuild MSO
# constatée le 05/07: « Liste des actes déconcentrés » 183->3 chunks).
_TABLE_REF_RE = re.compile(r"!?\[(tbl-[^\]]+)\]\([^)]*\)")


def _inline_ocr_tables(markdown: str, tables: list[dict]) -> str:
    """Remplace les références [tbl-N.md] par le contenu markdown de la table.

    mistral-ocr externalise les tableaux complexes: page["tables"] =
    [{"id": "tbl-0.md", "content": "| ... |\\n| --- |\\n| ... |"}]. Le contenu
    est un markdown de tableau propre, dé-pipé plus loin par _flatten_markdown_line
    au format « cellule cellule cellule » qu'attendent split_table_row et
    les heuristiques de process legacy.
    """
    if not tables:
        return markdown
    by_id = {str(t.get("id") or ""): str(t.get("content") or "") for t in tables if isinstance(t, dict)}

    def _replace(match: re.Match[str]) -> str:
        return by_id.get(match.group(1), match.group(0))

    return _TABLE_REF_RE.sub(_replace, markdown)


def _flatten_markdown_line(line: str) -> str:
    if _H1_MARKER_RE.match(line):
        line = "(Titre) " + _H1_MARKER_RE.sub("", line)
    elif _HEADING_MARKER_RE.match(line):
        line = "(Intertitre) " + re.sub(r"^#{2,6}\s+", "", line)
    line = _BOLD_RE.sub(r"\1", line)
    line = IMAGE_REF_RE.sub("", line)
    return line


def _repeated_page_lines(page_markdowns: list[str]) -> set[str]:
    """Letterhead/folios répétés de page en page (« MINISTÈRES SOCIAUX »,
    « Bulletin officiel … Page n », « RÉPUBLIQUE FRANÇAISE »…).

    Même principe que le _page_edge_boilerplate du silver heading-based, avec
    une fenêtre élargie: les slides tiennent en entier dans le letterhead
    (pages courtes = toute la page est un « bord »). Les headings markdown,
    lignes de tableaux, puces et refs [tbl-N.md] ne sont jamais candidats.
    """
    if len(page_markdowns) < 3:
        return set()
    counter: Counter[str] = Counter()
    for page in page_markdowns:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        window = lines if len(lines) <= 12 else lines[:6] + lines[-4:]
        for line in set(window):
            if 3 <= len(line) <= 80 and not line.startswith(("#", "|", "-", "•", "[", "!")):
                counter[line] += 1
    threshold = max(3, ceil(len(page_markdowns) * 0.4))
    return {line for line, count in counter.items() if count >= threshold}


def _parse_table_cells(rows: list[str]) -> list[list[str]]:
    parsed: list[list[str]] = []
    for row in rows:
        if _TABLE_SEPARATOR_ROW_RE.match(row):
            continue
        parsed.append([_BOLD_RE.sub(r"\1", cell).strip() for cell in row.strip().strip("|").split("|")])
    return parsed


def _is_matrix_table(parsed: list[list[str]]) -> bool:
    """Tableau-matrice: en-tête de groupes + sous-en-tête (1re cellule vide,
    libellés courts) et cellules « case cochée » dans les lignes de données.

    Tout autre tableau garde le dé-pipage legacy (« cellule cellule cellule »)
    dont dépendent les parseurs table_matrix/process."""
    if len(parsed) < 3:
        return False
    sub = parsed[1]
    if sub[0] or len(sub) < 3:
        return False
    labels = [cell for cell in sub[1:] if cell]
    if len(labels) < 2 or any(len(label) > 24 for label in labels):
        return False
    marks = sum(1 for row in parsed[2:] for cell in row[1:] if _MATRIX_MARK_RE.match(cell))
    return marks >= 2


def _matrix_headers(group_row: list[str], sub_row: list[str]) -> list[str]:
    headers: list[str] = []
    group = ""
    for j in range(max(len(group_row), len(sub_row))):
        g = group_row[j] if j < len(group_row) else ""
        s = sub_row[j] if j < len(sub_row) else ""
        if g:
            group = g.rstrip(" :")
        if s and group and j > 0:
            headers.append(f"{group} : {s}")
        else:
            headers.append(s or group)
    return headers


def _serialize_table_lines(rows: list[str]) -> list[str]:
    parsed = _parse_table_cells(rows)
    if not _is_matrix_table(parsed):
        # Dé-pipage legacy, à l'identique de l'aplatissement d'origine.
        return [" ".join(cell for cell in cells if cell) for cells in parsed if any(cells)]
    headers = _matrix_headers(parsed[0], parsed[1])
    out: list[str] = [" ".join(cell for cell in parsed[0] if cell)]
    for cells in parsed[2:]:
        label = cells[0]
        facts: list[str] = []
        for j, cell in enumerate(cells[1:], start=1):
            if not cell:
                continue
            header = headers[j] if j < len(headers) else ""
            if _MATRIX_MARK_RE.match(cell):
                facts.append(header or cell)
            elif header:
                facts.append(f"{header} : {cell}")
            else:
                facts.append(cell)
        if label and facts:
            out.append(f"{label} — {' ; '.join(facts)}")
        elif label or facts:
            out.append(label or " ; ".join(facts))
    return out


def flatten_ocr_to_text(ocr: OcrResult) -> str:
    """Markdown OCR -> texte plat au format attendu par les parseurs legacy
    (marqueurs de page [PAGE n] comme la sortie pdftotext des notebooks).

    Les tableaux externalisés par l'OCR (page["tables"]) sont réinjectés dans
    le flux avant aplatissement — indispensable aux modes table_matrix/process.
    """
    page_markdowns = [str(page.get("markdown") or "") for page in ocr.pages]
    if not any(md.strip() for md in page_markdowns):
        return normalize_text(_flatten_body(ocr.markdown or ""))
    boilerplate = _repeated_page_lines(page_markdowns)
    parts: list[str] = []
    for number, page in enumerate(ocr.pages, start=1):
        markdown = _inline_ocr_tables(str(page.get("markdown") or ""), page.get("tables") or [])
        body = _flatten_body(markdown, boilerplate)
        parts.append(f"[PAGE {number}]\n{body}" if len(ocr.pages) > 1 else body)
    return normalize_text("\n\n".join(parts))


def _flatten_body(markdown: str, boilerplate: set[str] = frozenset()) -> str:
    out: list[str] = []
    table_rows: list[str] = []

    def flush_table() -> None:
        if table_rows:
            out.extend(_serialize_table_lines(table_rows))
            table_rows.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            table_rows.append(stripped)
            continue
        if table_rows and not table_rows[-1].endswith("|") and stripped:
            # Cellule multi-lignes: la ligne physique continue la rangée ouverte.
            table_rows[-1] += " " + stripped
            continue
        flush_table()
        if stripped and stripped in boilerplate:
            continue
        out.append(_flatten_markdown_line(line))
    flush_table()
    return "\n".join(out)


def qna_section_uuid(identity: MinistryIdentity, doc_id: str, qa_id: str, section_index: int) -> str:
    """section_id stable par (doc, qa_id, index de section).

    Les qa_id legacy ne sont PAS uniques par document (MATTE: sha1 de la
    question seule — deux rubriques peuvent répéter la même question): sans
    le sel section_index, deux sections partageraient un section_id et
    l'upsert (doc_id, section_index) violerait la PK — le document entier
    partirait en erreur. qa_id et section_index étant déterministes, les
    re-runs produisent les mêmes ids."""
    return stable_uuid_from_parts(identity.namespace, doc_id, "section", qa_id, section_index)


class QnaSilverBuilder:
    """Silver QNA: contrat de sortie identique au HeadingSilverBuilder du socle
    (document + sections consommables par RagDbWriter), le payload QNA
    nécessaire au gold voyageant dans sections[].metadata.qna."""

    def __init__(self, identity: MinistryIdentity, engine_config: QnaEngineConfig):
        self.identity = identity
        self.engine_config = engine_config

    def build_bundle(self, asset: BronzeAsset) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = asset.row
        short_id = row.short_id
        doc_id = stable_uuid_from_parts(self.identity.namespace, self.identity.doc_source, short_id)
        source_name = str(row.cle_bucket).rsplit("/", 1)[-1] or f"{short_id}.pdf"
        theme = str(row.fields.get("theme") or "").strip()

        text = flatten_ocr_to_text(asset.ocr)
        mode, blocks, parse_coverage = parse_document(text, source_name, theme, self.engine_config)
        doc_text_hash = sha256_text(text)
        for block in blocks:
            block.section_id = qna_section_uuid(self.identity, doc_id, block.qa_id, block.section_index)

        doc_record = {
            "doc_id": doc_id,
            "source": self.identity.doc_source,
            "source_url": str(row.fields.get("url") or "").strip() or None,
            "storage_path": row.cle_bucket,
            "title": row.titre,
            "full_title": row.titre,
            "short_id": short_id,
            "publisher": self.identity.publisher,
            "doc_type": str(row.fields.get("type_document") or "").strip() or _MODE_DOC_TYPES.get(mode, "Guide RH"),
            "last_updated_date": None,
            "publication_date": row.date_publication,
            "page_count": asset.ocr.page_count or None,
            "lang": "fr",
            # checksum = sha256 du fichier d'origine (dropzone): contrat du
            # socle — c'est la clé du delta de réconciliation ignore_inchange.
            "checksum": asset.sha256,
            "parse_version": "qna_engine_v1",
            "parse_model": f"ocr_{asset.ocr.provider}_{asset.ocr.version}",
            "quality_flags": {
                "source_format": (row.cle_bucket.rsplit(".", 1)[-1].lower() if "." in row.cle_bucket else "pdf"),
                "ocr_provider": asset.ocr.provider,
                "ocr_from_cache": asset.ocr_from_cache,
                "parse_mode": mode,
                "parse_coverage": round(parse_coverage, 3),
                "section_aware": True,
            },
            "doc_markdown": text,
            "doc_markdown_raw": asset.ocr_markdown_raw or asset.ocr.markdown,
            "doc_text_hash": doc_text_hash,
            "token_count": count_tokens(text),
            "char_count": len(text),
            "line_count": text.count("\n") + (1 if text else 0),
            "metadata": {
                "corpus": row.corpus,
                "grist_record_id": row.record_id,
                "cle_bucket": row.cle_bucket,
                "theme": theme,
                "parse_mode": mode,
            },
            "doc_structure": {
                "section_count": len(blocks),
                "max_section_level": max((block.heading_level for block in blocks), default=0),
                "types": [f"{mode}_qna"],
            },
            "legacy_doc_id": None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

        section_records = [self._section_record(block, doc_id, doc_text_hash) for block in blocks]
        return doc_record, section_records

    def _section_record(self, block: SectionBlock, doc_id: str, doc_text_hash: str) -> dict[str, Any]:
        section_markdown = f"## {block.section_title}\n\n{block.answer}".strip()
        return {
            "section_id": block.section_id,
            "doc_id": doc_id,
            "heading": block.section_title,
            "heading_path": block.section_path,
            "section_markdown": section_markdown,
            "markdown_content": section_markdown,
            "section_index": block.section_index,
            "parent_section_id": None,
            "references_juridiques": [],
            "section_type": "qna",
            "level": block.heading_level,
            "page_start": None,
            "page_end": None,
            "token_count": count_tokens(section_markdown),
            "char_count": len(section_markdown),
            "text_hash": sha256_text(section_markdown),
            "doc_text_hash": doc_text_hash,
            "is_indexable": True,
            # Payload consommé par QnaGoldBuilder (clé ignorée par RagDbWriter
            # si la table n'a pas de colonne metadata: _prepare_rows filtre).
            # PAS d'answer ici: section_markdown la contient déjà (le gold la
            # dérive en retirant le préfixe « ## {heading}\n\n ») — la
            # dupliquer doublerait le poids des artefacts silver.
            "metadata": {
                "qna": {
                    "qa_id": block.qa_id,
                    "parent_qa_id": block.parent_qa_id,
                    "pseudo_question": block.pseudo_question,
                    "thematique": block.thematique,
                    "source_name": block.source_name,
                }
            },
        }

    def persist_bundle(self, repository: SilverRepository, asset: BronzeAsset) -> SilverBundle:
        document, sections = self.build_bundle(asset)
        short_id = document["short_id"]
        return SilverBundle(
            document=document,
            sections=sections,
            document_path=repository.save_document(short_id, document),
            sections_path=repository.save_sections(short_id, sections),
        )
