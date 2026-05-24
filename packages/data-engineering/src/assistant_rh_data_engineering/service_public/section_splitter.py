"""
Section Splitter pour RAG V3.

Découpe un doc_markdown en sections structurées en respectant :
- Les marqueurs <!-- PAGE: N -->
- Les blocs <!-- FIGURE_TEXT: fig_XXX --> ... <!-- /FIGURE_TEXT: fig_XXX -->
- La hiérarchie des headings Markdown (##, ###, etc.)

Usage:
    from assistant_rh_data_engineering.service_public.section_splitter import split_document_into_sections

    sections = split_document_into_sections(
        doc_markdown=doc_markdown,
        doc_id="uuid-xxx",
        doc_text_hash="hash-xxx"
    )
"""

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSTANTS & PATTERNS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SPLITTER_VERSION = "splitter_v2"

# Pattern pour les marqueurs de page
PAGE_MARKER_RE = re.compile(r'^<!--\s*PAGE:\s*(\d+)\s*-->$', re.MULTILINE)

# Pattern pour les blocs FIGURE_TEXT (ID permissif: lettres, chiffres, _, -)
FIGURE_BLOCK_RE = re.compile(
    r'(<!--\s*FIGURE_TEXT:\s*([A-Za-z0-9_\-]+)\s*-->.*?<!--\s*/FIGURE_TEXT:\s*\2\s*-->)',
    re.MULTILINE | re.DOTALL
)

# Pattern pour les headings Markdown (## à ###### - on ignore H1 car titre doc)
# Convention V3: le titre du doc est en ##, les sections commencent à ###
HEADING_RE = re.compile(r'^(#{2,6})\s+(.+?)\s*$', re.MULTILINE)


class SectionType(str, Enum):
    HEADING = "heading"
    FIGURE_TEXT = "figure_text"
    TABLE = "table"
    ANNEX = "annex"
    OTHER = "other"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATA CLASSES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PageOffset:
    """Mapping entre page et position dans le document."""
    page: int
    char_start: int
    char_end: int


@dataclass
class RawSection:
    """Section brute avant assignation des IDs et parents."""
    raw_index: int  # Index stable dans l'ordre du document
    level: int
    heading: str  # Texte du heading SANS les #
    section_type: SectionType
    content: str  # Contenu complet de la section (heading + body)
    char_start: int
    char_end: int
    figure_id: Optional[str] = None  # Pour les FIGURE_TEXT
    parent_raw_index: Optional[int] = None  # Index raw du parent


@dataclass
class Section:
    """Section finale prête pour insertion en DB."""
    section_index: int  # = raw_index (stable, ordre du doc)
    level: int
    section_type: str
    heading: Optional[str]
    heading_path: Optional[str]
    section_markdown: str
    char_start: int
    char_end: int
    page_start: Optional[int]
    page_end: Optional[int]
    token_count: int
    char_count: int
    text_hash: str
    parent_index: Optional[int] = None  # section_index du parent (= raw_index)
    is_indexable: bool = True  # False pour les sections trop courtes (mais on les garde)
    metadata: Dict[str, Any] = field(default_factory=dict)
    references_juridiques: List[Dict[str, Any]] = field(default_factory=list)  # Refs pour context expansion

    def to_db_dict(self, doc_id: str, doc_text_hash: str) -> Dict[str, Any]:
        """Convertit en dict pour insertion DB."""
        return {
            "doc_id": doc_id,
            "section_index": self.section_index,
            "level": self.level,
            "section_type": self.section_type,
            "heading": self.heading,
            "heading_path": self.heading_path,
            "section_markdown": self.section_markdown,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "token_count": self.token_count,
            "char_count": self.char_count,
            "text_hash": self.text_hash,
            "doc_text_hash": doc_text_hash,
            "metadata": self.metadata,
            "references_juridiques": self.references_juridiques,
            # parent_section_id sera résolu après insertion (on a parent_index)
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UTILITY FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_text_hash(text: str) -> str:
    """Calcule le hash SHA256 d'un texte."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# Import du module de références juridiques
try:
    from .legal_refs import (
        extract_and_parse_refs,
        refs_to_json,
    )
    LEGAL_REFS_AVAILABLE = True
except ImportError:
    LEGAL_REFS_AVAILABLE = False


def extract_inline_references(text: str) -> List[Dict[str, Any]]:
    """
    Extrait les références juridiques inline d'un texte Markdown.

    Utilise le module legal_refs pour une extraction et parsing complets:
    - Articles CGFP (L.332-2, R.123-4, D.456-7)
    - Décrets (n°86-83, article 34 du décret...)
    - Références internes (ANNEXE 5, FICHE 8)
    - Liens Légifrance explicites

    Returns:
        Liste de dicts compatibles avec rag_sections.references_juridiques
    """
    if LEGAL_REFS_AVAILABLE:
        # Utiliser le nouveau module complet
        refs = extract_and_parse_refs(text)
        return refs_to_json(refs)
    else:
        # Fallback: extraction simple (liens Légifrance uniquement)
        import re
        refs = []
        pattern = r'\[([^\]]+)\]\((https?://www\.legifrance\.gouv\.fr[^\)]+)\)'
        for match in re.finditer(pattern, text, re.IGNORECASE):
            refs.append({
                "titre": match.group(1).strip(),
                "url": match.group(2),
                "source": "inline"
            })
        return refs


def count_tokens(text: str) -> int:
    """Estimation simple du nombre de tokens (~4.2 chars/token)."""
    if not text:
        return 0
    return int(len(text) / 4.2)


def extract_page_offsets(doc_markdown: str) -> List[PageOffset]:
    """
    Extrait les offsets de chaque page depuis les marqueurs <!-- PAGE: N -->.

    Returns:
        Liste de PageOffset avec page, char_start, char_end
    """
    offsets = []
    matches = list(PAGE_MARKER_RE.finditer(doc_markdown))

    for i, match in enumerate(matches):
        page = int(match.group(1))
        char_start = match.end()  # Après le marqueur

        # char_end = début du prochain marqueur ou fin du document
        if i + 1 < len(matches):
            char_end = matches[i + 1].start()
        else:
            char_end = len(doc_markdown)

        offsets.append(PageOffset(page=page, char_start=char_start, char_end=char_end))

    return offsets


def get_page_for_offset(char_pos: int, page_offsets: List[PageOffset]) -> Optional[int]:
    """Trouve la page correspondant à une position de caractère."""
    for po in page_offsets:
        if po.char_start <= char_pos < po.char_end:
            return po.page
    # Si avant le premier marqueur ou après le dernier
    if page_offsets and char_pos < page_offsets[0].char_start:
        return page_offsets[0].page
    if page_offsets and char_pos >= page_offsets[-1].char_start:
        return page_offsets[-1].page
    return None


def get_page_range(char_start: int, char_end: int, page_offsets: List[PageOffset]) -> Tuple[Optional[int], Optional[int]]:
    """Trouve la plage de pages pour une section."""
    if not page_offsets:
        return None, None

    page_start = get_page_for_offset(char_start, page_offsets)
    page_end = get_page_for_offset(char_end - 1, page_offsets)  # -1 car char_end est exclusif

    return page_start, page_end


def extract_figure_blocks(doc_markdown: str) -> List[Dict[str, Any]]:
    """
    Extrait tous les blocs FIGURE_TEXT du document.

    Returns:
        Liste de {figure_id, content, char_start, char_end}
    """
    blocks = []
    for match in FIGURE_BLOCK_RE.finditer(doc_markdown):
        blocks.append({
            "figure_id": match.group(2),
            "content": match.group(1),
            "char_start": match.start(),
            "char_end": match.end(),
        })
    return blocks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN SPLITTING LOGIC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _remove_figure_blocks_from_content(content: str, figure_blocks: List[Dict], section_start: int, section_end: int) -> Tuple[str, List[str]]:
    """
    Retire les blocs FIGURE_TEXT du contenu et les remplace par des placeholders.

    Returns:
        (content_sans_figures, liste_des_figure_ids_retirés)
    """
    figure_ids = []
    result = content

    section_figures = [
        fb
        for fb in figure_blocks
        if fb["char_start"] >= section_start and fb["char_end"] <= section_end
    ]
    for fb in sorted(section_figures, key=lambda item: item["char_start"], reverse=True):
        # Position relative dans le contenu original. Processing from right to left
        # keeps these offsets valid while replacing blocks in result.
        rel_start = fb["char_start"] - section_start
        rel_end = fb["char_end"] - section_start

        if rel_start >= 0 and rel_end <= len(result):
            figure_ids.append(fb["figure_id"])
            placeholder = f'<!-- FIGURE_REF: {fb["figure_id"]} -->'
            result = result[:rel_start] + placeholder + result[rel_end:]

    figure_ids.reverse()
    return result, figure_ids


def find_all_section_boundaries(doc_markdown: str) -> List[RawSection]:
    """
    Trouve toutes les frontières de sections dans le document.

    Une section commence à un heading et se termine au heading suivant
    de même niveau ou plus haut, ou à la fin du document.

    Les blocs FIGURE_TEXT sont extraits comme sections séparées et remplacés
    par des placeholders dans les sections parentes (évite la duplication).
    """
    raw_sections: List[RawSection] = []

    # 1) Extraire les blocs FIGURE_TEXT (ils ont priorité)
    figure_blocks = extract_figure_blocks(doc_markdown)
    figure_ranges = [(fb["char_start"], fb["char_end"]) for fb in figure_blocks]

    def is_in_figure_block(pos: int) -> bool:
        """Vérifie si une position est dans un bloc FIGURE_TEXT."""
        for start, end in figure_ranges:
            if start <= pos < end:
                return True
        return False

    # 2) Trouver tous les headings (en dehors des FIGURE_TEXT)
    headings = []
    for match in HEADING_RE.finditer(doc_markdown):
        if not is_in_figure_block(match.start()):
            level = len(match.group(1))  # Nombre de #
            heading_text = match.group(2).strip()
            headings.append({
                "level": level,
                "heading": heading_text,
                "char_start": match.start(),
                "match_end": match.end(),
            })

    # 3) Créer les sections à partir des headings
    heading_sections = []
    for i, h in enumerate(headings):
        # Trouver la fin de cette section
        # = début du prochain heading de niveau <= actuel, ou fin du doc
        section_end = len(doc_markdown)

        for j in range(i + 1, len(headings)):
            next_h = headings[j]
            if next_h["level"] <= h["level"]:
                section_end = next_h["char_start"]
                break

        raw_content = doc_markdown[h["char_start"]:section_end]

        # Retirer les FIGURE_TEXT du contenu (remplacés par placeholders)
        content, contained_figure_ids = _remove_figure_blocks_from_content(
            raw_content, figure_blocks, h["char_start"], section_end
        )
        content = content.strip()

        heading_sections.append({
            "level": h["level"],
            "heading": h["heading"],
            "section_type": SectionType.HEADING,
            "content": content,
            "char_start": h["char_start"],
            "char_end": section_end,
            "contained_figure_ids": contained_figure_ids,
        })

    # 4) Ajouter les FIGURE_TEXT comme sections séparées
    figure_sections = []
    for fb in figure_blocks:
        # Extraire le titre du logigramme depuis le contenu (première ligne de heading)
        lines = fb["content"].split('\n')
        heading = f"Figure {fb['figure_id']}"
        for line in lines:
            if line.strip().startswith('###'):
                # Extraire le texte après ###
                title_match = re.match(r'^###\s*[^—]*—\s*(.+?)(?:\s*\(page|\s*$)', line.strip())
                if title_match:
                    heading = title_match.group(1).strip()
                else:
                    # Fallback: prendre tout après ###
                    heading = re.sub(r'^###\s*', '', line.strip())[:100]
                break

        figure_sections.append({
            "level": 3,  # Niveau arbitraire pour les figures
            "heading": heading,
            "section_type": SectionType.FIGURE_TEXT,
            "content": fb["content"],
            "char_start": fb["char_start"],
            "char_end": fb["char_end"],
            "figure_id": fb["figure_id"],
        })

    # 5) Combiner et trier par position
    all_sections = heading_sections + figure_sections
    all_sections.sort(key=lambda s: s["char_start"])

    # 6) Gérer le contenu avant le premier heading (préambule)
    if all_sections:
        first_start = all_sections[0]["char_start"]
        if first_start > 0:
            preamble = doc_markdown[:first_start].strip()
            preamble_clean = PAGE_MARKER_RE.sub('', preamble).strip()
            if preamble_clean:
                all_sections.insert(0, {
                    "level": 2,  # Niveau 2 pour être cohérent avec la convention
                    "heading": "Introduction",
                    "section_type": SectionType.OTHER,
                    "content": preamble,
                    "char_start": 0,
                    "char_end": first_start,
                })
    elif doc_markdown.strip():
        # Pas de headings du tout, le doc entier est une section
        all_sections.append({
            "level": 2,
            "heading": "Document",
            "section_type": SectionType.OTHER,
            "content": doc_markdown,
            "char_start": 0,
            "char_end": len(doc_markdown),
        })

    # 7) Convertir en RawSection avec raw_index stable
    for idx, s in enumerate(all_sections):
        raw_sections.append(RawSection(
            raw_index=idx,
            level=s["level"],
            heading=s["heading"],
            section_type=s["section_type"],
            content=s["content"],
            char_start=s["char_start"],
            char_end=s["char_end"],
            figure_id=s.get("figure_id"),
        ))

    # 8) Calculer parent_raw_index pour chaque section
    for i, section in enumerate(raw_sections):
        # Chercher en arrière le premier heading de niveau < actuel
        for j in range(i - 1, -1, -1):
            if raw_sections[j].level < section.level:
                section.parent_raw_index = raw_sections[j].raw_index
                break

    return raw_sections


def build_heading_path(sections: List[RawSection], current_index: int) -> str:
    """
    Construit le chemin breadcrumb pour une section.
    Ex: "Fiche 1 > Chapitre 2 > Les congés"

    Utilise parent_raw_index pour remonter la hiérarchie.
    """
    if current_index < 0 or current_index >= len(sections):
        return ""

    # Créer un dict pour accès rapide par raw_index
    by_index = {s.raw_index: s for s in sections}

    current = sections[current_index]
    path_parts = [current.heading or "Sans titre"]

    # Remonter la chaîne des parents
    parent_idx = current.parent_raw_index
    while parent_idx is not None and parent_idx in by_index:
        parent = by_index[parent_idx]
        path_parts.insert(0, parent.heading or "Sans titre")
        parent_idx = parent.parent_raw_index

    return " > ".join(path_parts)


def split_document_into_sections(
    doc_markdown: str,
    doc_id: str,
    doc_text_hash: str,
    *,
    min_section_chars: int = 50,
    doc_references: Optional[List[Dict[str, Any]]] = None,
    extract_inline_refs: bool = True,
) -> List[Section]:
    """
    Découpe un document en sections structurées.

    Args:
        doc_markdown: Le contenu Markdown du document
        doc_id: UUID du document (pour référence)
        doc_text_hash: Hash du doc_markdown (pour invalidation)
        min_section_chars: Seuil pour marquer is_indexable=False (mais on garde tout)
        doc_references: Références juridiques globales du document (propagées à toutes les sections)
        extract_inline_refs: Si True, extrait aussi les références inline du texte de chaque section

    Returns:
        Liste de Section prêtes pour insertion en DB.
        section_index = raw_index (stable, ordre du document).
        Les sections trop courtes ont is_indexable=False mais sont conservées.
    """
    if not doc_markdown or not doc_markdown.strip():
        return []

    # Références globales du document (optionnelles)
    global_refs = doc_references or []

    # 1) Extraire les offsets de page
    page_offsets = extract_page_offsets(doc_markdown)

    # 2) Trouver les sections brutes
    raw_sections = find_all_section_boundaries(doc_markdown)

    # 3) Convertir en sections finales (on garde TOUT, section_index = raw_index)
    sections: List[Section] = []

    for raw in raw_sections:
        # Calculer les pages
        page_start, page_end = get_page_range(raw.char_start, raw.char_end, page_offsets)

        # Construire le heading_path
        heading_path = build_heading_path(raw_sections, raw.raw_index)

        # Déterminer si indexable (pour le retrieval)
        is_indexable = True
        if raw.section_type != SectionType.FIGURE_TEXT and len(raw.content) < min_section_chars:
            is_indexable = False

        # Metadata additionnelle
        metadata = {
            "splitter_version": SPLITTER_VERSION,
        }
        if raw.figure_id:
            metadata["figure_id"] = raw.figure_id

        # === RÉFÉRENCES JURIDIQUES ===
        # Combiner les refs globales du document + les refs inline de la section
        section_refs = list(global_refs)  # Copie des refs globales

        if extract_inline_refs:
            inline_refs = extract_inline_references(raw.content)
            # Dédupliquer par URL/titre
            seen = {(r.get("url"), r.get("titre")) for r in section_refs}
            for ref in inline_refs:
                key = (ref.get("url"), ref.get("titre"))
                if key not in seen:
                    seen.add(key)
                    section_refs.append(ref)

        section = Section(
            section_index=raw.raw_index,  # = raw_index (stable!)
            level=raw.level,
            section_type=raw.section_type.value,
            heading=raw.heading,
            heading_path=heading_path,
            section_markdown=raw.content,
            char_start=raw.char_start,
            char_end=raw.char_end,
            page_start=page_start,
            page_end=page_end,
            token_count=count_tokens(raw.content),
            char_count=len(raw.content),
            text_hash=compute_text_hash(raw.content),
            parent_index=raw.parent_raw_index,  # Direct, pas de remapping!
            is_indexable=is_indexable,
            metadata=metadata,
            references_juridiques=section_refs,
        )
        sections.append(section)

    return sections


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONVENIENCE FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_section_summary(sections: List[Section]) -> Dict[str, Any]:
    """
    Retourne un résumé des sections pour debug/UI.
    """
    indexable = [s for s in sections if s.is_indexable]

    return {
        "total_sections": len(sections),
        "indexable_sections": len(indexable),
        "non_indexable_sections": len(sections) - len(indexable),
        "by_type": {
            "heading": len([s for s in sections if s.section_type == "heading"]),
            "figure_text": len([s for s in sections if s.section_type == "figure_text"]),
            "table": len([s for s in sections if s.section_type == "table"]),
            "annex": len([s for s in sections if s.section_type == "annex"]),
            "other": len([s for s in sections if s.section_type == "other"]),
        },
        "by_level": {
            level: len([s for s in sections if s.level == level])
            for level in range(1, 7)
            if any(s.level == level for s in sections)
        },
        "total_tokens": sum(s.token_count for s in sections),
        "total_chars": sum(s.char_count for s in sections),
        "avg_tokens_per_section": sum(s.token_count for s in sections) // max(1, len(sections)),
        "splitter_version": SPLITTER_VERSION,
    }


def sections_to_tree(sections: List[Section]) -> List[Dict[str, Any]]:
    """
    Convertit la liste plate de sections en arbre hiérarchique.
    Utile pour l'affichage dans l'UI.
    """
    # Créer un dict pour accès rapide par section_index
    section_dict = {s.section_index: {
        "section": s,
        "children": []
    } for s in sections}

    roots = []

    for s in sections:
        node = section_dict[s.section_index]
        if s.parent_index is not None and s.parent_index in section_dict:
            section_dict[s.parent_index]["children"].append(node)
        else:
            roots.append(node)

    def node_to_dict(node: Dict) -> Dict[str, Any]:
        s = node["section"]
        return {
            "index": s.section_index,
            "level": s.level,
            "type": s.section_type,
            "heading": s.heading,
            "heading_path": s.heading_path,
            "token_count": s.token_count,
            "is_indexable": s.is_indexable,
            "pages": f"{s.page_start or '?'}-{s.page_end or '?'}",
            "children": [node_to_dict(c) for c in node["children"]],
        }

    return [node_to_dict(r) for r in roots]


def print_section_tree(sections: List[Section], indent: int = 0) -> str:
    """
    Affiche l'arbre des sections sous forme de texte indenté.
    """
    tree = sections_to_tree(sections)
    lines = []

    def print_node(node: Dict, level: int = 0):
        prefix = "  " * level
        heading = node["heading"] or "(sans titre)"
        idx_flag = "" if node["is_indexable"] else " [skip]"
        lines.append(f"{prefix}[{node['index']}] L{node['level']} {node['type']}: {heading} ({node['token_count']} tok, p.{node['pages']}){idx_flag}")
        for child in node["children"]:
            print_node(child, level + 1)

    for root in tree:
        print_node(root)

    return "\n".join(lines)
