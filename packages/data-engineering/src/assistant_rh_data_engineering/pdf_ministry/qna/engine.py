"""Heuristiques et parseurs QNA — portage des notebooks legacy MATTE/MSO.

Chaque fonction est portée du notebook de référence (extract_pdf_MSO.ipynb sauf
mention MATTE) à comportement identique: ces heuristiques ont produit les
chunks legacy sur lesquels le goldset est calé. Toute évolution se documente
comme divergence, comme pour le parsing des modules mi/masa.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def sha1_u(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_token_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class SectionBlock:
    """Une section logique Q/R: la brique commune silver->gold du moteur QNA."""

    qa_id: str
    parent_qa_id: str | None
    parent_section_path: str | None
    section_path: str
    section_index: int
    heading_level: int
    section_title: str
    pseudo_question: str
    answer: str
    source_name: str
    thematique: str
    # Rattachement DB, rempli par les builders silver/gold — PAS par les
    # parseurs. Voyage avec le bloc jusqu'aux chunks: les qa_id legacy ne sont
    # pas uniques par document (MATTE: sha1 de la question seule — deux
    # rubriques peuvent répéter la même question), un mapping qa_id->section
    # écraserait en last-wins.
    section_id: str | None = None


@dataclass(kw_only=True)
class QnaEngineConfig:
    """Réglages par corpus du moteur QNA (fidélité au legacy de chacun).

    - modes: ordre de routage. MATTE legacy essayait d'abord les marqueurs
      explicites Q:/questions (parse_qna_blocks) puis les headings; MSO route
      table_matrix -> faq -> process -> guide. "auto" = détection MSO.
    - chunk_format: "qr" (MATTE: 'Q: ...\\nR: ...') ou "titre_section"
      (MSO: 'Titre: ...\\nSection: ...\\nQuestion utilisateur probable: ...').
    - composite_max_chars: troncature du QA_COMPOSITE (1500 MATTE, 3000 MSO).
    - emit_table_chunks: rôle TABLE sur les paragraphes tabulaires (MATTE).
    - extra_heading_patterns: patterns de headings additionnels pour le mode
      guide (MATTE: FICHE n / ANNEXE n).
    """

    modes: tuple[str, ...] = ("table_matrix", "faq", "process", "guide")
    chunk_format: str = "titre_section"
    composite_max_chars: int = 3000
    chunk_max_chars: int = 1200
    chunk_overlap: int = 200
    emit_table_chunks: bool = False
    extra_heading_patterns: tuple[tuple[int, str], ...] = ()
    # Garde-fou de couverture (divergence Phase D): un mode dont les blocs
    # capturent moins de cette fraction du contenu est écarté (mode suivant,
    # puis bloc fallback à 100 %). Voir parse_document.
    min_parse_coverage: float = 0.35


# ---------------------------------------------------------------------------
# Découpage de texte (identique dans les deux notebooks)
# ---------------------------------------------------------------------------


def hard_wrap(text: str, max_chars: int, overlap: int) -> list[str]:
    result: list[str] = []
    i = 0
    step = max(1, max_chars - overlap)
    while i < len(text):
        result.append(text[i : i + max_chars])
        i += step
    return result


def split_on_paragraphs(text: str, max_chars: int = 1200, overlap: int = 200) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    out: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        extra = 2 if buffer else 0
        if len(buffer) + extra + len(paragraph) <= max_chars:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        else:
            if buffer:
                out.append(buffer)
            if len(paragraph) > max_chars:
                out.extend(hard_wrap(paragraph, max_chars, overlap))
                buffer = ""
            else:
                buffer = paragraph
    if buffer:
        out.append(buffer)
    return out


# ---------------------------------------------------------------------------
# Tables des matières et headings (notebook MSO)
# ---------------------------------------------------------------------------

TOC_START_PAT = re.compile(r"^table des matieres$", re.IGNORECASE)
TOC_ENTRY_PAT = re.compile(r"^(?:[IVXLC]+-|[A-Z]\.|\d+\.)?.{3,}\.{5,}\s*\d+\s*$", re.IGNORECASE)
HEADING_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (1, re.compile(r"^\((?P<label>Titre)\)\s*(?P<title>.+)$", re.IGNORECASE)),
    (2, re.compile(r"^\((?P<label>Intertitre)\)\s*(?P<title>.+)$", re.IGNORECASE)),
    (1, re.compile(r"^(?P<label>[IVXLC]+-)\s*(?P<title>.+)$")),
    (2, re.compile(r"^(?P<label>[A-Z]\.)\s*(?P<title>.+)$")),
    (2, re.compile(r"^(?P<label>\d+\s*:)\s*(?P<title>.+)$")),
    (3, re.compile(r"^(?P<label>\d+\.[a-z])\s*(?P<title>.+)$")),
    (3, re.compile(r"^(?P<label>\d+\.)\s*(?P<title>.+)$")),
    (4, re.compile(r"^(?P<label>[a-z]\.)\s*(?P<title>.+)$")),
    (4, re.compile(r"^(?P<label>[✓✔☑])\s*(?P<title>.+)$")),
]


def strip_table_of_contents(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_toc = False
    toc_hits = 0
    for raw in lines:
        stripped = raw.strip()
        if TOC_START_PAT.match(stripped):
            in_toc = True
            toc_hits = 0
            continue
        if in_toc:
            if TOC_ENTRY_PAT.match(stripped) or stripped in {"", "[PAGE 2]", "[PAGE 3]"}:
                toc_hits += 1 if TOC_ENTRY_PAT.match(stripped) else 0
                continue
            if toc_hits >= 3:
                in_toc = False
            else:
                out.append(raw)
                continue
        out.append(raw)
    return normalize_text("\n".join(out))


def is_strong_synthetic_heading(clean: str) -> bool:
    letters = [c for c in clean if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio >= 0.75


def detect_heading(
    line: str,
    synthetic_heading_mode: str = "normal",
    extra_patterns: tuple[tuple[int, str], ...] = (),
):
    clean = re.sub(r"\s+", " ", line).strip()
    clean = re.sub(r"\.{4,}\s*\d+$", "", clean).strip()
    if not clean or clean.startswith("[PAGE ") or clean.startswith("[SLIDE "):
        return None
    for level, pattern in HEADING_PATTERNS:
        m = pattern.match(clean)
        if m:
            title = m.group("title").strip(" -:\t")
            if title:
                return level, m.group("label"), title
    for level, raw_pattern in extra_patterns:
        m = re.match(raw_pattern, clean, re.IGNORECASE)
        if m:
            groups = m.groupdict()
            title = (groups.get("title") or clean).strip(" -–:\t")
            if title:
                return level, groups.get("label") or "X.", title
    if clean.endswith("?") and len(clean) <= 140 and len(clean.split()) <= 14:
        return 2, "Q.", clean.strip(" ?")
    if (
        synthetic_heading_mode != "none"
        and (synthetic_heading_mode != "strict" or is_strong_synthetic_heading(clean))
        and len(clean) <= 90
        and 2 <= len(clean.split()) <= 10
        and clean[0].isupper()
        and not clean.startswith("-")
        and clean[-1] not in ".;,"
    ):
        return 2, "H.", clean.strip()
    return None


def looks_like_heading_continuation(line: str) -> bool:
    clean = re.sub(r"\s+", " ", line).strip()
    if not clean or detect_heading(clean, synthetic_heading_mode="none"):
        return False
    if len(clean) > 160:
        return False
    return clean[0].islower() or clean.startswith("(")


# ---------------------------------------------------------------------------
# FAQ (notebook MSO)
# ---------------------------------------------------------------------------

FAQ_NUMBERED_LINE_RE = re.compile(r"^(?P<label>\d{1,2})\s*\.\s*(?P<title>.+)$")
FAQ_TOC_LEADER_RE = re.compile(r"\.{5,}\s*\d+\s*$")


def has_toc_leader(line: str) -> bool:
    return bool(FAQ_TOC_LEADER_RE.search(re.sub(r"\s+", " ", line or "").strip()))


def is_faq_toc_entry_line(line: str) -> bool:
    clean = re.sub(r"\s+", " ", line or "").strip()
    if has_toc_leader(clean):
        return True
    return bool(FAQ_NUMBERED_LINE_RE.match(clean) and re.search(r"\?\s+\d{1,3}$", clean))


def strip_faq_number_prefix(line: str) -> tuple[str | None, str]:
    clean = re.sub(r"\s+", " ", line or "").strip()
    clean = re.sub(r"\.{5,}\s*\d+\s*$", "", clean).strip()
    m = FAQ_NUMBERED_LINE_RE.match(clean)
    if not m:
        return None, clean
    return m.group("label"), m.group("title").strip(" -:\t")


def is_probable_faq_section_line(line: str) -> bool:
    _, title = strip_faq_number_prefix(line)
    if not title or not FAQ_NUMBERED_LINE_RE.match(re.sub(r"\s+", " ", line or "").strip()) or is_faq_toc_entry_line(line):
        return False
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio >= 0.65 and len(title.split()) >= 2


def is_faq_question_line(line: str) -> bool:
    _, title = strip_faq_number_prefix(line)
    if not title or is_faq_toc_entry_line(line) or is_probable_faq_section_line(line):
        return False
    return "?" in title and len(title.split()) >= 3


def strip_faq_leading_toc(text: str) -> str:
    lines = text.split("\n")
    window = [line.strip() for line in lines[:240] if line.strip()]
    if sum(1 for line in window if is_faq_toc_entry_line(line)) < 6:
        return text
    for i, raw in enumerate(lines[:600]):
        line = raw.strip()
        if not is_faq_question_line(line):
            continue
        page_start = 0
        for j in range(i - 1, -1, -1):
            if lines[j].strip().startswith("[PAGE "):
                page_start = j + 1
                break
        start = i
        for j in range(page_start, i):
            candidate = lines[j].strip()
            if is_probable_faq_section_line(candidate):
                start = j
                break
        return normalize_text("\n".join(lines[start:]))
    return text


def iter_faq_logical_lines(text: str) -> list[str]:
    cleaned = strip_faq_leading_toc(strip_table_of_contents(text))
    raw_lines = [line.strip() for line in cleaned.split("\n")]
    logical: list[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if not line or line.startswith("[PAGE ") or line.startswith("[SLIDE ") or (line.isdigit() and len(line) <= 3) or is_faq_toc_entry_line(line):
            i += 1
            continue
        if FAQ_NUMBERED_LINE_RE.match(line):
            parts = [line]
            j = i + 1
            if is_probable_faq_section_line(line):
                while j < len(raw_lines):
                    nxt = raw_lines[j].strip()
                    if not nxt or nxt.startswith("[PAGE ") or FAQ_NUMBERED_LINE_RE.match(nxt) or is_faq_toc_entry_line(nxt):
                        break
                    parts.append(nxt)
                    j += 1
            else:
                while "?" not in " ".join(parts) and j < len(raw_lines):
                    nxt = raw_lines[j].strip()
                    if not nxt or nxt.startswith("[PAGE ") or FAQ_NUMBERED_LINE_RE.match(nxt) or is_faq_toc_entry_line(nxt):
                        break
                    parts.append(nxt)
                    j += 1
            logical.append(normalize_text(" ".join(parts)))
            i = j
            continue
        logical.append(line)
        i += 1
    return logical


def looks_like_faq_text(text: str, source_name: str = "") -> bool:
    source_hint = "faq" in normalize_token_text(source_name)
    text_hint = "faq" in normalize_token_text(text[:1200])
    lines = iter_faq_logical_lines(text)
    question_hits = sum(1 for line in lines if is_faq_question_line(line))
    section_hits = sum(1 for line in lines if is_probable_faq_section_line(line))
    return question_hits >= 5 and (source_hint or text_hint or (question_hits >= 10 and section_hits >= 2))


# ---------------------------------------------------------------------------
# Inférence de questions (notebook MSO)
# ---------------------------------------------------------------------------


def infer_user_question(section_title: str, section_path: str) -> str:
    title = re.sub(r"\s+", " ", section_title).strip(" .")
    if not title:
        return "Quelles sont les regles applicables dans cette section ?"
    if title.lower().startswith(("le ", "la ", "les ", "l'")):
        return f"Quelles sont les regles relatives a {title.lower()} ?"
    if any(token in title.lower() for token in ["procedure", "renouvellement", "recrutement", "conge", "temps partiel", "remuneration"]):
        return f"Quelle est la procedure ou les regles concernant {title.lower()} ?"
    if section_path:
        return f"Que faut-il savoir sur {title.lower()} dans le cadre de {section_path.lower()} ?"
    return f"Que faut-il savoir sur {title.lower()} ?"


def infer_process_question(step_title: str, section_path: str, branch_label: str | None = None, actor_label: str | None = None) -> str:
    title = re.sub(r"\s+", " ", step_title).strip(" .")
    if branch_label and actor_label:
        return f"Que doit faire {actor_label.lower()} pour {title.lower()} dans le cas {branch_label.lower()} ?"
    if branch_label:
        return f"Que faut-il faire pour {title.lower()} dans le cas {branch_label.lower()} ?"
    if actor_label:
        return f"Quel est le role de {actor_label.lower()} pour {title.lower()} ?"
    if section_path:
        return f"Quelle est l'etape ou la regle concernant {title.lower()} dans le processus ?"
    return f"Que faut-il savoir sur {title.lower()} ?"


def infer_table_question(section_title: str, act_name: str, entity: str, alinea: str | None = None) -> str:
    act_norm = act_name.strip().rstrip(" .")
    if alinea and alinea != "-":
        return f"Quelle entite de gestion est competente pour {act_norm.lower()} au titre de l'alinea {alinea} ?"
    return f"Quelle entite de gestion est competente pour {act_norm.lower()} dans la rubrique {section_title.lower()} ?"


# ---------------------------------------------------------------------------
# Process / logigrammes (notebook MSO)
# ---------------------------------------------------------------------------

PROCESS_ACTOR_HINTS = (
    "sgcd",
    "dreets",
    "ddets",
    "drhm",
    "cbcm",
    "dgfip",
    "renoirh",
    "prefet",
    "agent",
    "services deconcentres",
    "service recruteur",
)
PROCESS_STEP_HINTS = (
    "publication",
    "selection",
    "production",
    "signature",
    "depot",
    "validation",
    "creation",
    "mise en paie",
    "pre liquidation",
    "simulation",
    "recrutement",
)
PROCESS_BRANCH_HINTS = {
    "si respect rdr": "Respect du RDR",
    "respect rdr": "Respect du RDR",
    "si hors rdr": "Hors RDR",
    "hors rdr": "Hors RDR",
    "si refus cbcm": "Refus du CBCM",
    "si visa cbcm": "Visa du CBCM",
}
BOILERPLATE_LINES = {
    "secrétariat général",
    "secrétariat general",
    "direction des ressources humaines",
    "c1 - public",
}


def is_boilerplate_line(line: str) -> bool:
    norm = normalize_token_text(line)
    if not norm:
        return True
    if norm in BOILERPLATE_LINES:
        return True
    if re.fullmatch(r"\d{1,2}/\d{2}/\d{4}", norm):
        return True
    if norm.isdigit() and len(norm) <= 3:
        return True
    return False


def merge_process_fragments(lines: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("[PAGE ") or line.startswith("[SLIDE "):
            merged.append(line)
            i += 1
            continue
        parts = [line]
        if len(line.split()) <= 3:
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or nxt.startswith("[PAGE ") or nxt.startswith("[SLIDE "):
                    break
                if len(nxt.split()) > 3 or len(parts) >= 3:
                    break
                parts.append(nxt)
                j += 1
            merged.append(" ".join(parts))
            i = j
            continue
        merged.append(line)
        i += 1
    return merged


def canonical_branch_label(line: str) -> str | None:
    norm = normalize_token_text(line)
    return PROCESS_BRANCH_HINTS.get(norm)


def canonical_actor_label(line: str) -> str | None:
    norm = normalize_token_text(line)
    if any(hint in norm for hint in PROCESS_ACTOR_HINTS):
        return line.strip()
    return None


def is_process_step_title(line: str) -> bool:
    norm = normalize_token_text(line)
    if is_boilerplate_line(line) or canonical_branch_label(line) or canonical_actor_label(line):
        return False
    if len(line) > 110 or len(line.split()) < 2 or len(line.split()) > 12:
        return False
    if line.startswith("-") or line.endswith(":"):
        return False
    if any(hint in norm for hint in PROCESS_STEP_HINTS):
        return True
    return line[0].isupper() and line[-1] not in ".;,"


# ---------------------------------------------------------------------------
# Matrices tabulaires « Liste des actes déconcentrés » (notebook MSO)
# ---------------------------------------------------------------------------

TABLE_SECTION_TITLES = {
    "propositions financières": "Propositions financières",
    "signature des contrats et avenants": "Signature des contrats et avenants",
    "fin du contrat": "Fin du contrat",
    "modalités de service": "Modalités de service",
    "formation et concours": "Formation et concours",
    "absences et congés": "Absences et congés",
    "disciplinaire": "Disciplinaire",
    "notice": "Notice",
}
TABLE_ENTITY_LABELS = (
    "Déconcentré",
    "DRH-BPECO",
    "DRH-BPECO / CBCM",
    "CBCM",
)
TABLE_SECTION_TITLES_NORM_MAP = {normalize_token_text(k): v for k, v in TABLE_SECTION_TITLES.items()}
TABLE_SECTION_TITLES_NORM = set(TABLE_SECTION_TITLES_NORM_MAP.keys())


def looks_like_table_matrix_text(text: str) -> bool:
    norm = normalize_text(text).lower()
    if "type d'actes" not in norm:
        return False
    entity_hit = "entité de gestion" in norm or "entite de gestion" in norm
    degree_hits = len(re.findall(r"\d+°", norm))
    entity_values = sum(norm.count(label) for label in ("déconcentré", "deconcentre", "drh-bpeco", "cbcm"))
    section_hits = sum(
        norm.count(label)
        for label in (
            "modalités de service",
            "modalites de service",
            "absences et congés",
            "absences et conges",
            "disciplinaire",
            "propositions financières",
            "propositions financieres",
        )
    )
    return entity_hit and ((degree_hits >= 5 and entity_values >= 5) or (entity_values >= 8 and section_hits >= 2))


def canonical_table_entity(line: str) -> str | None:
    stripped = re.sub(r"\s+", " ", line).strip(" -")
    norm = normalize_token_text(stripped)
    for label in TABLE_ENTITY_LABELS:
        if normalize_token_text(label) == norm:
            return label
    return None


def is_table_section_heading(line: str) -> bool:
    return normalize_token_text(line) in TABLE_SECTION_TITLES_NORM


def canonical_table_section_heading(line: str) -> str | None:
    norm = normalize_token_text(line)
    if norm in TABLE_SECTION_TITLES_NORM_MAP:
        return TABLE_SECTION_TITLES_NORM_MAP[norm]
    for key, label in TABLE_SECTION_TITLES_NORM_MAP.items():
        if norm.startswith(key + " ") or norm.startswith(key + " •"):
            return label
    return None


def split_table_row(line: str) -> tuple[str, str, str] | None:
    compact = re.sub(r"\s+", " ", line).strip()
    m = re.match(r"^(?P<act>.+?)\s+(?P<alinea>(?:\d+°|-))\s+(?P<entity>Déconcentré|DRH-BPECO(?: / CBCM)?|CBCM)$", compact)
    if not m:
        return None
    return m.group("act").strip(), m.group("alinea").strip(), m.group("entity").strip()


def split_table_row_with_trailing_notice(line: str) -> tuple[tuple[str, str, str] | None, str | None]:
    compact = re.sub(r"\s+", " ", line).strip()
    m = re.match(
        r"^(?P<act>.+?)\s+(?P<alinea>(?:\d+°|-))\s+(?P<entity>Déconcentré|DRH-BPECO(?: / CBCM)?|CBCM)(?:\s+(?P<tail>.+))?$",
        compact,
    )
    if not m:
        return None, None
    row = (m.group("act").strip(), m.group("alinea").strip(), m.group("entity").strip())
    tail = (m.group("tail") or "").strip() or None
    return row, tail


# ---------------------------------------------------------------------------
# Marqueurs explicites Q/R (notebook MATTE — parse_qna_blocks)
# ---------------------------------------------------------------------------

MATTE_Q_PAT = re.compile(
    r"^(?:Q(?:uestion)?\s*[:\-]\s*|"
    r"(?:Quel(?:le|s)?|Comment|Pourquoi|Quand|Dans quel(?:le)?|L'agent|Le contractuel|Vous)\b.*\?\s*$|"
    r"(?:Quelle est la procédure|Le contractuel a-t-il droit|L'agent a-t-il droit)\b.*)",
    re.IGNORECASE,
)
MATTE_HEAD_PAT = re.compile(
    r"^[IVXLC]+[\.\-]\s+|"  # I.  II-  etc.
    r"^\d+[\.\)]\s+|"  # 1.  2)  etc.
    r"^(FICHE\s*\d+)\b|"  # FICHE 3
    r"^(ANNEXE\s*\d+)\b",  # ANNEXE 1
    re.IGNORECASE,
)
MATTE_TABLE_HINT = re.compile(r"^(Tableau\b|.*\|.*\|.*)$", re.IGNORECASE)
MATTE_SUBQ = re.compile(
    r"(procédure|préavis|montant|conditions|conséquences|droit|indemnité|reclassement|"
    r"cas particulier|délais|pièces|modalités|exceptions?)",
    re.IGNORECASE,
)

# Patterns FICHE/ANNEXE au format detect_heading, pour le mode guide MATTE.
MATTE_EXTRA_HEADING_PATTERNS: tuple[tuple[int, str], ...] = (
    (1, r"^(?P<label>FICHE\s*\d+)\s*[-–:]*\s*(?P<title>.+)$"),
    (1, r"^(?P<label>ANNEXE\s*\d+)\s*[-–:]*\s*(?P<title>.+)$"),
)


def looks_like_qna_markers_text(text: str) -> bool:
    """Pré-filtre du mode marqueurs explicites: au moins UNE ligne-question au
    sens MATTE (Q_PAT) — parité avec le notebook legacy, qui retenait
    parse_qna_blocks dès qu'il produisait un bloc. Le garde-fou de couverture
    de parse_document protège des routages faméliques."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return any(MATTE_Q_PAT.match(line) for line in lines)


def parse_qna_markers_blocks(text: str, source_name: str, thematique: str) -> list[SectionBlock]:
    """Portage MATTE parse_qna_blocks: une ligne-question ouvre un bloc, les
    lignes suivantes forment la réponse jusqu'à la prochaine question ou un
    heading. Les sous-questions (lexique SUBQ) se rattachent à la dernière
    question racine de la même section."""
    lines = text.split("\n")
    section_stack: list[str] = []
    blocks: list[SectionBlock] = []
    current_q: str | None = None
    current_ans: list[str] = []
    section_path = ""
    section_counter = 0
    last_root: dict[str, str] = {}

    def flush() -> None:
        nonlocal current_q, current_ans, section_counter
        if current_q is None:
            return
        answer = normalize_text("\n".join(current_ans))
        qnorm = re.sub(r"\s+", " ", current_q).strip().lower()
        qa_id = sha1_u(qnorm)
        is_sub = bool(MATTE_SUBQ.search(current_q or ""))
        parent_qa_id = None
        if is_sub and section_path in last_root:
            parent_qa_id = last_root[section_path]
        else:
            last_root[section_path] = qa_id
        section_counter += 1
        question = current_q.strip()
        blocks.append(
            SectionBlock(
                qa_id=qa_id,
                parent_qa_id=parent_qa_id,
                parent_section_path=section_path or None,
                section_path=f"{section_path} > {question}" if section_path else question,
                section_index=section_counter,
                heading_level=3,
                section_title=question,
                pseudo_question=question if question.endswith("?") else f"{question}?",
                answer=answer,
                source_name=source_name,
                thematique=thematique,
            )
        )
        current_q, current_ans = None, []

    for raw in lines:
        line = raw.strip()
        if not line:
            if current_q is not None:
                current_ans.append("")
            continue
        if MATTE_HEAD_PAT.match(line) and not MATTE_Q_PAT.match(line):
            title = re.sub(r"^[IVXLC]+[\.\-]\s+|\d+[\.\)]\s+", "", line).strip()
            if title:
                section_stack.append(title)
                section_stack[:] = section_stack[-4:]
                section_path = " > ".join(section_stack)
            continue
        if MATTE_Q_PAT.match(line):
            flush()
            current_q = line
            current_ans = []
        elif current_q is not None:
            current_ans.append(line)

    flush()
    return [block for block in blocks if block.answer]


# ---------------------------------------------------------------------------
# Parseurs MSO (guide / faq / process / table_matrix)
# ---------------------------------------------------------------------------


def parse_guide_blocks(
    text: str,
    source_name: str,
    thematique: str,
    extra_heading_patterns: tuple[tuple[int, str], ...] = (),
) -> list[SectionBlock]:
    cleaned = strip_table_of_contents(text)
    lines = cleaned.split("\n")
    content_lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith(("[PAGE ", "[SLIDE "))]
    true_heading_hits = sum(
        1
        for line in content_lines
        if (heading := detect_heading(line, synthetic_heading_mode="none", extra_patterns=extra_heading_patterns)) and heading[1] not in {"Q.", "H."}
    )
    structured_marker_hits = sum(1 for line in content_lines if re.match(r"^\((?:titre|intertitre)\)", line, re.IGNORECASE))
    synthetic_heading_mode = "none" if structured_marker_hits >= 2 else ("strict" if true_heading_hits >= 2 else "normal")
    blocks: list[SectionBlock] = []
    stack: list[dict] = []
    current: dict | None = None
    section_counter = 0

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        answer = normalize_text("\n".join(current["body"]))
        if answer:
            blocks.append(
                SectionBlock(
                    qa_id=current["qa_id"],
                    parent_qa_id=current["parent_qa_id"],
                    parent_section_path=current["parent_section_path"],
                    section_path=current["section_path"],
                    section_index=current["section_index"],
                    heading_level=current["level"],
                    section_title=current["title"],
                    pseudo_question=current["pseudo_question"],
                    answer=answer,
                    source_name=source_name,
                    thematique=thematique,
                )
            )
        current = None

    for raw in lines:
        line = raw.strip()
        if not line:
            if current:
                current["body"].append("")
            continue
        heading = detect_heading(line, synthetic_heading_mode=synthetic_heading_mode, extra_patterns=extra_heading_patterns)
        if heading:
            flush_current()
            level, _, title = heading
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            parent_qa_id = stack[-1]["qa_id"] if stack else None
            parent_section_path = " > ".join(x["title"] for x in stack) if stack else None
            section_counter += 1
            qa_id = sha1_u(f"{source_name}|guide|{section_counter}|{level}|{' > '.join(x['title'] for x in stack)}|{title}")
            section_titles = [x["title"] for x in stack] + [title]
            section_path = " > ".join(section_titles)
            pseudo_question = infer_user_question(title, section_path)
            current = {
                "qa_id": qa_id,
                "parent_qa_id": parent_qa_id,
                "parent_section_path": parent_section_path,
                "level": level,
                "title": title,
                "section_path": section_path,
                "section_index": section_counter,
                "pseudo_question": pseudo_question,
                "body": [],
            }
            stack.append({"level": level, "title": title, "qa_id": qa_id})
            continue
        if current is None:
            continue
        if not current["body"] and looks_like_heading_continuation(line):
            current["title"] = normalize_text(f"{current['title']} {line.strip(' -')}")
            if stack and stack[-1]["qa_id"] == current["qa_id"]:
                stack[-1]["title"] = current["title"]
            parent_titles = [x["title"] for x in stack[:-1]]
            current["parent_section_path"] = " > ".join(parent_titles) if parent_titles else None
            current["section_path"] = " > ".join(parent_titles + [current["title"]])
            current["pseudo_question"] = infer_user_question(current["title"], current["section_path"])
            continue
        current["body"].append(line)

    flush_current()
    return blocks


def parse_faq_blocks(text: str, source_name: str, thematique: str) -> list[SectionBlock]:
    lines = iter_faq_logical_lines(text)
    blocks: list[SectionBlock] = []
    section_counter = 0
    current_section: str | None = None
    current: dict | None = None

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        answer = normalize_text("\n".join(current["body"]))
        if answer:
            blocks.append(
                SectionBlock(
                    qa_id=current["qa_id"],
                    parent_qa_id=None,
                    parent_section_path=current["parent_section_path"],
                    section_path=current["section_path"],
                    section_index=current["section_index"],
                    heading_level=current["level"],
                    section_title=current["title"],
                    pseudo_question=current["pseudo_question"],
                    answer=answer,
                    source_name=source_name,
                    thematique=thematique,
                )
            )
        current = None

    for line in lines:
        if is_probable_faq_section_line(line):
            flush_current()
            _, title = strip_faq_number_prefix(line)
            current_section = title
            continue
        if is_faq_question_line(line):
            flush_current()
            section_counter += 1
            _, title = strip_faq_number_prefix(line)
            question = title if title.endswith("?") else f"{title}?"
            section_path = f"{current_section} > {title}" if current_section else title
            qa_id = sha1_u(f"{source_name}|faq|{section_counter}|{current_section}|{title}")
            current = {
                "qa_id": qa_id,
                "parent_section_path": current_section,
                "level": 3,
                "title": title,
                "section_path": section_path,
                "section_index": section_counter,
                "pseudo_question": question,
                "body": [],
            }
            continue
        if current:
            current["body"].append(line)

    flush_current()
    return blocks


def parse_process_blocks(text: str, source_name: str, thematique: str) -> list[SectionBlock]:
    cleaned = strip_table_of_contents(text)
    raw_lines = list(cleaned.split("\n"))
    lines = [line for line in merge_process_fragments(raw_lines) if line.strip()]
    blocks: list[SectionBlock] = []
    section_counter = 0
    current_heading: str | None = None
    current_branch: str | None = None
    current_step: dict | None = None

    def flush_step() -> None:
        nonlocal current_step
        if not current_step:
            return
        answer_lines = []
        if current_step.get("branch"):
            answer_lines.append(f"Branche: {current_step['branch']}")
        if current_step.get("actor"):
            answer_lines.append(f"Acteur principal: {current_step['actor']}")
        answer_lines.extend(current_step.get("body") or [])
        answer = normalize_text("\n".join(answer_lines)) or current_step["title"]
        blocks.append(
            SectionBlock(
                qa_id=current_step["qa_id"],
                parent_qa_id=None,
                parent_section_path=current_step.get("parent_section_path"),
                section_path=current_step["section_path"],
                section_index=current_step["section_index"],
                heading_level=2,
                section_title=current_step["title"],
                pseudo_question=current_step["pseudo_question"],
                answer=answer,
                source_name=source_name,
                thematique=thematique,
            )
        )
        current_step = None

    for line in lines:
        if is_boilerplate_line(line) or line.startswith("[PAGE ") or line.startswith("[SLIDE "):
            continue
        heading = detect_heading(line)
        if heading and heading[2].lower() not in {"q", "h"}:
            flush_step()
            current_heading = heading[2]
            current_branch = None
            continue
        branch_label = canonical_branch_label(line)
        if branch_label:
            flush_step()
            current_branch = branch_label
            continue
        actor_label = canonical_actor_label(line)
        if actor_label and current_step:
            current_step["actor"] = current_step.get("actor") or actor_label
            current_step["pseudo_question"] = infer_process_question(
                current_step["title"], current_step["section_path"], current_step.get("branch"), current_step.get("actor")
            )
            current_step["body"].append(actor_label)
            continue
        if is_process_step_title(line):
            flush_step()
            section_counter += 1
            parent_section_path = current_heading or None
            section_parts = [current_heading] if current_heading else []
            if current_branch:
                section_parts.append(current_branch)
            section_parts.append(line)
            section_path = " > ".join(part for part in section_parts if part)
            qa_id = sha1_u(f"{source_name}|process|{current_heading}|{current_branch}|{line}|{section_counter}")
            current_step = {
                "qa_id": qa_id,
                "title": line,
                "branch": current_branch,
                "actor": None,
                "body": [],
                "section_index": section_counter,
                "section_path": section_path,
                "parent_section_path": parent_section_path,
                "pseudo_question": infer_process_question(line, section_path, current_branch, None),
            }
            continue
        if current_step:
            current_step["body"].append(line)

    flush_step()
    return blocks


def parse_table_matrix_blocks(text: str, source_name: str, thematique: str) -> list[SectionBlock]:
    cleaned = strip_table_of_contents(text)
    lines = [line.strip() for line in cleaned.split("\n") if line.strip() and not line.startswith("[PAGE ") and not line.startswith("[SLIDE ")]
    blocks: list[SectionBlock] = []
    section_counter = 0
    current_heading: str | None = None
    pending_parts: list[str] = []
    pending_alinea: str | None = None
    in_notice = False
    notice_lines: list[str] = []

    def flush_pending(entity: str | None = None) -> None:
        nonlocal pending_parts, pending_alinea, section_counter
        if not pending_parts:
            pending_alinea = None
            return
        act_name = normalize_text(" ".join(pending_parts))
        pending_parts = []
        if not act_name or not entity:
            pending_alinea = None
            return
        section_counter += 1
        effective_heading = current_heading or Path(source_name).stem
        section_path = f"{effective_heading} > {act_name}" if effective_heading else act_name
        qa_id = sha1_u(f"{source_name}|table|{effective_heading}|{act_name}|{pending_alinea}|{entity}|{section_counter}")
        answer_lines = [
            f"Rubrique: {effective_heading}",
            f"Type d'acte: {act_name}",
            f"Alinéa de référence: {pending_alinea or '-'}",
            f"Entité de gestion: {entity}",
        ]
        if notice_lines:
            answer_lines.append("Notice: " + " ".join(notice_lines[:2]))
        blocks.append(
            SectionBlock(
                qa_id=qa_id,
                parent_qa_id=None,
                parent_section_path=effective_heading if current_heading else None,
                section_path=section_path,
                section_index=section_counter,
                heading_level=2,
                section_title=act_name,
                pseudo_question=infer_table_question(effective_heading, act_name, entity, pending_alinea),
                answer=normalize_text("\n".join(answer_lines)),
                source_name=source_name,
                thematique=thematique,
            )
        )
        pending_alinea = None

    for line in lines:
        norm = normalize_token_text(line)
        if norm in {"type d'actes", "entite de gestion"}:
            continue
        if line == "Notice":
            flush_pending()
            in_notice = True
            continue
        if norm in {"type d'actes entite de gestion", "entite de gestion type d'actes"}:
            continue
        if in_notice and (
            line.startswith("•") or norm.startswith("la colonne") or norm.startswith("a gauche") or norm.startswith("les autres feuilles")
        ):
            notice_lines.append(line.lstrip("• "))
            continue
        if norm.startswith("references :") or norm.startswith("arretes du") or norm.startswith("les actes devront etre") or norm == "attention !":
            continue
        direct_row, trailing_notice = split_table_row_with_trailing_notice(line)
        if direct_row:
            flush_pending()
            pending_parts = [direct_row[0]]
            pending_alinea = direct_row[1]
            flush_pending(direct_row[2])
            if trailing_notice and (
                trailing_notice.startswith("•")
                or trailing_notice.startswith("La colonne")
                or trailing_notice.startswith("A gauche")
                or trailing_notice.startswith("Les autres feuilles")
                or trailing_notice.startswith("niveau du")
            ):
                notice_lines.append(trailing_notice.lstrip("• "))
            continue
        heading_label = canonical_table_section_heading(line)
        if heading_label and heading_label != current_heading:
            flush_pending()
            current_heading = heading_label
            in_notice = False
            remainder = line[len(heading_label) :].strip(" :-•")
            if remainder and (
                remainder.startswith("La colonne")
                or remainder.startswith("A gauche")
                or remainder.startswith("Les autres feuilles")
                or remainder.startswith("•")
            ):
                notice_lines.append(remainder.lstrip("• "))
            continue
        entity = canonical_table_entity(line)
        if entity:
            flush_pending(entity)
            continue
        if re.fullmatch(r"(?:\d+°|-)", line):
            pending_alinea = line
            continue
        if is_boilerplate_line(line):
            continue
        pending_parts.append(line)

    flush_pending()
    if not blocks:
        return parse_process_blocks(text, source_name, thematique)
    return blocks


# ---------------------------------------------------------------------------
# Routage + point d'entrée du parsing
# ---------------------------------------------------------------------------


def detect_document_mode(text: str, source_name: str = "") -> str:
    """Routage MSO d'origine (table_matrix -> faq -> process -> guide)."""
    cleaned = strip_table_of_contents(text)
    if looks_like_table_matrix_text(cleaned):
        return "table_matrix"
    if looks_like_faq_text(cleaned, source_name):
        return "faq"
    lines = [line.strip() for line in cleaned.split("\n") if line.strip() and not line.startswith("[PAGE ") and not line.startswith("[SLIDE ")]
    if not lines:
        return "guide"
    guide_heading_hits = sum(1 for line in lines if (heading := detect_heading(line)) and heading[1] not in {"Q.", "H."})
    merged = merge_process_fragments(lines)
    short_ratio = sum(1 for line in merged if len(line.split()) <= 4) / max(1, len(merged))
    long_lines = sum(1 for line in merged if len(line.split()) >= 12)
    process_hits = sum(1 for line in merged if is_process_step_title(line) or canonical_branch_label(line) or canonical_actor_label(line))
    explicit_process = any(token in normalize_token_text(source_name) for token in ("processus", "logigramme"))
    explicit_process = explicit_process or "logigramme" in normalize_token_text(cleaned[:1500])
    if guide_heading_hits >= 2 and not explicit_process:
        return "guide"
    if explicit_process or (short_ratio >= 0.35 and process_hits >= 8 and long_lines <= max(6, len(merged) // 6)):
        return "process"
    return "guide"


def dedupe_section_blocks(blocks: list[SectionBlock]) -> list[SectionBlock]:
    deduped: list[SectionBlock] = []
    seen: set[tuple[str, str, str, str]] = set()
    for block in blocks:
        key = (block.source_name, block.section_path, block.pseudo_question, block.answer)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    for index, block in enumerate(deduped, start=1):
        block.section_index = index
    return deduped


def _content_length(text: str) -> int:
    """Longueur du contenu hors marqueurs de page/slide (dénominateur du
    garde-fou de couverture)."""
    return sum(len(line) for line in text.split("\n") if line.strip() and not line.strip().startswith(("[PAGE ", "[SLIDE ")))


def _blocks_coverage(blocks: list[SectionBlock], content_length: int) -> float:
    if content_length <= 0:
        return 1.0
    captured = sum(len(block.answer) + len(block.section_title) for block in blocks)
    return min(1.0, captured / content_length)


def parse_document(
    text: str,
    source_name: str,
    thematique: str,
    config: QnaEngineConfig,
) -> tuple[str, list[SectionBlock], float]:
    """Route un document vers son parseur selon config.modes et retourne
    (mode retenu, blocs dédupliqués). Chaîne de fallback fidèle aux notebooks:
    un mode qui ne produit rien passe au suivant; en dernier recours un bloc
    unique couvre tout le document (aucun texte ne se perd).

    Garde-fou de couverture (divergence Phase D, audit rebuild MSO du 05/07):
    les parseurs legacy peuvent JETER l'essentiel du contenu quand la
    linéarisation OCR ne colle pas à leurs attentes (logigrammes « process »:
    85 % du texte présent, 0 % dans les chunks). Un mode dont les blocs
    capturent moins de min_parse_coverage du contenu est écarté au profit du
    mode suivant; si aucun mode n'atteint le plancher, le bloc fallback couvre
    100 % du document. Le comportement legacy est intact quand il fonctionne.
    """
    text = normalize_text(text)
    content_length = _content_length(text)
    blocks: list[SectionBlock] = []
    mode_used = "fallback"
    coverage = 0.0

    for mode in config.modes:
        if mode == "qna_markers":
            if not looks_like_qna_markers_text(text):
                continue
            candidate = parse_qna_markers_blocks(text, source_name, thematique)
        elif mode == "table_matrix":
            if not looks_like_table_matrix_text(strip_table_of_contents(text)):
                continue
            candidate = parse_table_matrix_blocks(text, source_name, thematique)
        elif mode == "faq":
            if not looks_like_faq_text(strip_table_of_contents(text), source_name):
                continue
            candidate = parse_faq_blocks(text, source_name, thematique)
        elif mode == "process":
            if detect_document_mode(text, source_name) != "process":
                continue
            candidate = parse_process_blocks(text, source_name, thematique)
        elif mode == "guide":
            candidate = parse_guide_blocks(text, source_name, thematique, extra_heading_patterns=config.extra_heading_patterns)
        else:
            raise ValueError(f"Mode de parsing QNA inconnu: {mode!r}")
        if not candidate:
            continue
        candidate_coverage = _blocks_coverage(candidate, content_length)
        if candidate_coverage >= config.min_parse_coverage:
            blocks = candidate
            mode_used = mode
            coverage = candidate_coverage
            break
        # Mode écarté (couverture sous le plancher): mode suivant, puis
        # fallback à 100 % — on ne garde jamais un partiel qui perd le contenu.

    if not blocks and text:
        first_line = next((line.strip() for line in text.split("\n") if line.strip()), "")
        title = first_line or Path(source_name).stem or "Document sans titre"
        blocks = [
            SectionBlock(
                qa_id=sha1_u(f"{source_name}|fallback"),
                parent_qa_id=None,
                parent_section_path=None,
                section_path=title,
                section_index=1,
                heading_level=1,
                section_title=title,
                pseudo_question=infer_user_question(title, title),
                answer=text,
                source_name=source_name,
                thematique=thematique,
            )
        ]
        mode_used = "fallback"
        coverage = 1.0

    return mode_used, dedupe_section_blocks(blocks), coverage


@dataclass
class QnaChunk:
    """Chunk QNA avant enrichissement par le gold builder.

    Pas de hash_id ici: le seed du contrat d'identité des chunks appartient à
    utils/gold.build_chunk_row seul (un fork du seed entre dédup interne et
    hash persisté ferait diverger les deux sans erreur). La dédup de
    section_blocks_to_chunks utilise le tuple équivalent au seed.
    """

    qa_id: str
    parent_qa_id: str | None
    role: str
    section_path: str
    chunk_index: int
    text: str
    source_name: str
    thematique: str
    section_id: str | None = None


def section_blocks_to_chunks(blocks: list[SectionBlock], config: QnaEngineConfig) -> list[QnaChunk]:
    """Blocs -> chunks QNA, au format du corpus (fidèle au notebook d'origine).

    - "titre_section" (MSO): Q_ONLY idx 0, QA_COMPOSITE idx 1 (tronqué),
      A_ATOMIC idx 2+ — préfixe 'Titre:/Section:/Question utilisateur probable:'.
    - "qr" (MATTE): Q_ONLY idx 0 (question brute), QA_COMPOSITE idx 1
      ('Q: ...\\n\\nR: ...' tronqué), A_ATOMIC ('Q: {q<=160}\\nR: {piece}'),
      puis TABLE sur les paragraphes tabulaires si emit_table_chunks.
    """
    rows: list[QnaChunk] = []
    for block in blocks:
        q = block.pseudo_question
        a = block.answer

        if config.chunk_format == "titre_section":
            prefix = f"Titre: {block.section_title}\nSection: {block.section_path}"
            q_text = f"{prefix}\nQuestion utilisateur probable: {q}"
            rows.append(
                QnaChunk(
                    qa_id=block.qa_id,
                    parent_qa_id=block.parent_qa_id,
                    role="Q_ONLY",
                    section_path=block.section_path,
                    chunk_index=0,
                    text=q_text,
                    source_name=block.source_name,
                    thematique=block.thematique,
                    section_id=block.section_id,
                )
            )
            composite = f"{prefix}\nQuestion utilisateur probable: {q}\n\nContenu:\n{a}"[: config.composite_max_chars]
            rows.append(
                QnaChunk(
                    qa_id=block.qa_id,
                    parent_qa_id=block.parent_qa_id or block.qa_id,
                    role="QA_COMPOSITE",
                    section_path=block.section_path,
                    chunk_index=1,
                    text=composite,
                    source_name=block.source_name,
                    thematique=block.thematique,
                    section_id=block.section_id,
                )
            )
            next_index = 2
            for piece in split_on_paragraphs(a, max_chars=config.chunk_max_chars, overlap=config.chunk_overlap):
                atomic = f"Titre: {block.section_title}\nSection: {block.section_path}\nQuestion utilisateur probable: {q}\n\nContenu:\n{piece}"
                rows.append(
                    QnaChunk(
                        qa_id=block.qa_id,
                        parent_qa_id=block.parent_qa_id or block.qa_id,
                        role="A_ATOMIC",
                        section_path=block.section_path,
                        chunk_index=next_index,
                        text=atomic,
                        source_name=block.source_name,
                        thematique=block.thematique,
                        section_id=block.section_id,
                    )
                )
                next_index += 1

        elif config.chunk_format == "qr":
            question = block.section_title.strip()
            if question:
                rows.append(
                    QnaChunk(
                        qa_id=block.qa_id,
                        parent_qa_id=block.parent_qa_id,
                        role="Q_ONLY",
                        section_path=block.section_path,
                        chunk_index=0,
                        text=question,
                        source_name=block.source_name,
                        thematique=block.thematique,
                        section_id=block.section_id,
                    )
                )
            parent_for_children = block.parent_qa_id or block.qa_id
            next_index = 1
            if a:
                composite = f"Q: {question}\n\nR: {a}" if question else a
                rows.append(
                    QnaChunk(
                        qa_id=block.qa_id,
                        parent_qa_id=parent_for_children,
                        role="QA_COMPOSITE",
                        section_path=block.section_path,
                        chunk_index=1,
                        text=composite[: config.composite_max_chars],
                        source_name=block.source_name,
                        thematique=block.thematique,
                        section_id=block.section_id,
                    )
                )
                next_index = 2
                q_short = re.sub(r"\s+", " ", question)[:160]
                for piece in split_on_paragraphs(a, config.chunk_max_chars, config.chunk_overlap):
                    atomic = f"Q: {q_short}\nR: {piece}"
                    rows.append(
                        QnaChunk(
                            qa_id=block.qa_id,
                            parent_qa_id=parent_for_children,
                            role="A_ATOMIC",
                            section_path=block.section_path,
                            chunk_index=next_index,
                            text=atomic,
                            source_name=block.source_name,
                            thematique=block.thematique,
                            section_id=block.section_id,
                        )
                    )
                    next_index += 1
                if config.emit_table_chunks:
                    for paragraph in re.split(r"\n{2,}", a):
                        p = (paragraph or "").strip()
                        if p and MATTE_TABLE_HINT.match(p):
                            rows.append(
                                QnaChunk(
                                    qa_id=block.qa_id,
                                    parent_qa_id=parent_for_children,
                                    role="TABLE",
                                    section_path=block.section_path,
                                    chunk_index=next_index,
                                    text=p,
                                    source_name=block.source_name,
                                    thematique=block.thematique,
                                    section_id=block.section_id,
                                )
                            )
                            next_index += 1
        else:
            raise ValueError(f"chunk_format inconnu: {config.chunk_format!r}")

    deduped: list[QnaChunk] = []
    seen: set[tuple[str, str, str, int, str]] = set()
    for row in rows:
        key = (row.source_name, row.qa_id, row.role, row.chunk_index, row.text[:256])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped
