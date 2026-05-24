from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

Q_PAT = re.compile(
    r"^(?:Q(?:uestion)?\s*[:\-]\s*|"
    r"(?:Quel(?:le|s)?|Comment|Pourquoi|Quand|Dans quel(?:le)?|"
    r"L'agent|Le contractuel|Vous)\b.*\?\s*$|"
    r"(?:Quelle est la procédure|Le contractuel a-t-il droit|"
    r"L'agent a-t-il droit)\b.*)",
    re.IGNORECASE,
)

HEAD_PAT = re.compile(
    r"^(?:#{1,6}\s+|"
    r"[IVXLC]+[\.\-]\s+|"
    r"\d+[\.\)]\s+|"
    r"(FICHE\s*\d+)\b|"
    r"(ANNEXE\s*\d+)\b)",
    re.IGNORECASE,
)

TABLE_HINT = re.compile(r"^(Tableau\b|.*\|.*\|.*)$", re.IGNORECASE)

SUBQ = re.compile(
    r"(procédure|préavis|montant|conditions|conséquences|droit|indemnité|reclassement|"
    r"cas particulier|délais|pièces|modalités|exceptions?)",
    re.IGNORECASE,
)


@dataclass
class NotebookChunk:
    qa_id: str
    parent_qa_id: Optional[str]
    role: str
    section_path: str
    chunk_index: int
    text: str
    source_name: str
    lang: str = "fr"
    thematique: str = ""


def norm(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha1_u(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _strip_heading_prefix(text: str) -> str:
    return re.sub(
        r"^#{1,6}\s+|"
        r"^[IVXLC]+[\.\-]\s+|"
        r"^\d+[\.\)]\s+|"
        r"(FICHE\s*\d+\s*[-–:]*)|"
        r"(ANNEXE\s*\d+\s*[-–:]*)",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def parse_qna_blocks(text: str) -> List[Dict]:
    lines = text.split("\n")
    section_stack: List[str] = []
    blocks: List[Dict] = []
    current_q: Optional[str] = None
    current_ans: List[str] = []
    section_path = ""
    last_root: Dict[str, str] = {}

    def flush() -> None:
        nonlocal current_q, current_ans, section_path
        if current_q is None:
            return
        ans = "\n".join(current_ans).strip()
        qnorm = re.sub(r"\s+", " ", current_q).strip().lower()
        qa_id = sha1_u(qnorm)

        is_sub = bool(SUBQ.search(current_q or ""))
        parent_qa_id = None
        if is_sub and section_path in last_root:
            parent_qa_id = last_root[section_path]
        else:
            last_root[section_path] = qa_id

        blocks.append(
            {
                "qa_id": qa_id,
                "parent_qa_id": parent_qa_id,
                "section_path": section_path,
                "question": (current_q or "").strip(),
                "answer": ans,
            }
        )
        current_q, current_ans = None, []

    for raw in lines:
        line = raw.strip()

        if not line:
            if current_q is not None:
                current_ans.append("")
            continue

        if HEAD_PAT.match(line) and not Q_PAT.match(line):
            title = _strip_heading_prefix(line)
            if title:
                section_stack.append(title)
                section_stack[:] = section_stack[-4:]
                section_path = " > ".join(section_stack)
            continue

        if Q_PAT.match(line):
            flush()
            current_q = line
            current_ans = []
        else:
            if current_q is not None:
                current_ans.append(line)

    flush()
    return blocks


def parse_heading_blocks(text: str) -> List[Dict]:
    lines = text.split("\n")
    section_stack: List[str] = []
    blocks: List[Dict] = []
    current_q: Optional[str] = None
    current_ans: List[str] = []
    section_path = ""

    def flush() -> None:
        nonlocal current_q, current_ans, section_path
        if current_q is None:
            return
        ans = "\n".join(current_ans).strip()
        if not ans:
            current_q, current_ans = None, []
            return

        qnorm = re.sub(r"\s+", " ", current_q).strip().lower()
        qa_id = sha1_u(qnorm)
        blocks.append(
            {
                "qa_id": qa_id,
                "parent_qa_id": None,
                "section_path": section_path,
                "question": current_q.strip(),
                "answer": ans,
            }
        )
        current_q, current_ans = None, []

    for raw in lines:
        line = raw.strip()
        if not line:
            if current_q is not None:
                current_ans.append("")
            continue

        if HEAD_PAT.match(line):
            flush()
            title = _strip_heading_prefix(line) or line
            section_stack.append(title)
            section_stack[:] = section_stack[-4:]
            section_path = " > ".join(section_stack)
            current_q = title
            current_ans = []
        else:
            if current_q is not None:
                current_ans.append(line)

    flush()
    return blocks


def parse_blocks_with_fallback(text: str) -> List[Dict]:
    blocks = parse_qna_blocks(text)
    if blocks:
        return blocks

    blocks = parse_heading_blocks(text)
    if blocks:
        return blocks

    stripped = text.strip()
    if not stripped:
        return []

    first_line = ""
    for line in text.split("\n"):
        candidate = line.strip()
        if candidate:
            first_line = candidate
            break

    question = first_line or "Document sans titre"
    qnorm = re.sub(r"\s+", " ", question).strip().lower()
    qa_id = sha1_u(qnorm)
    return [
        {
            "qa_id": qa_id,
            "parent_qa_id": None,
            "section_path": "",
            "question": question,
            "answer": stripped,
        }
    ]


def hard_wrap(text: str, max_chars: int, overlap: int) -> List[str]:
    if max_chars <= 0:
        return [text]
    res: List[str] = []
    i = 0
    n = len(text)
    step = max(1, max_chars - overlap)
    while i < n:
        res.append(text[i : i + max_chars])
        i += step
    return res


def split_on_paragraphs(text: str, max_chars: int, overlap: int) -> List[str]:
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    out: List[str] = []
    buf = ""
    for para in paras:
        extra = 2 if buf else 0
        if len(buf) + extra + len(para) <= max_chars:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                out.append(buf)
            if len(para) > max_chars:
                out.extend(hard_wrap(para, max_chars, overlap))
                buf = ""
            else:
                buf = para
    if buf:
        out.append(buf)

    final: List[str] = []
    for chunk in out:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            final.extend(hard_wrap(chunk, max_chars, overlap))
    return final


def make_chunks(
    blocks: List[Dict],
    source_name: str,
    thematique: str = "",
    max_chars: int = 1200,
    overlap: int = 200,
) -> List[NotebookChunk]:
    rows: List[NotebookChunk] = []
    for block in blocks:
        question = (block["question"] or "").strip()
        answer = (block["answer"] or "").strip()
        qa_id = block["qa_id"]
        parent = block.get("parent_qa_id")
        section = block["section_path"]

        if question:
            rows.append(
                NotebookChunk(
                    qa_id,
                    parent,
                    "Q_ONLY",
                    section,
                    0,
                    question,
                    source_name,
                    thematique=thematique,
                )
            )

        parent_for_children = parent or qa_id
        if answer:
            composite = f"Q: {question}\n\nR: {answer}" if question else answer
            rows.append(
                NotebookChunk(
                    qa_id,
                    parent_for_children,
                    "QA_COMPOSITE",
                    section,
                    1,
                    composite[:1500],
                    source_name,
                    thematique=thematique,
                )
            )
            next_idx = 2

            last_index = next_idx - 1
            answer_chunks = split_on_paragraphs(answer, max_chars, overlap)
            for idx, chunk in enumerate(answer_chunks, start=next_idx):
                q_short = re.sub(r"\s+", " ", question)[:160]
                rows.append(
                    NotebookChunk(
                        qa_id,
                        parent_for_children,
                        "A_ATOMIC",
                        section,
                        idx,
                        f"Q: {q_short}\nR: {chunk}",
                        source_name,
                        thematique=thematique,
                    )
                )
                last_index = idx
            next_idx = last_index + 1

            for para in re.split(r"\n{2,}", answer):
                value = (para or "").strip()
                if value and TABLE_HINT.match(value):
                    rows.append(
                        NotebookChunk(
                            qa_id,
                            parent_for_children,
                            "TABLE",
                            section,
                            next_idx,
                            value,
                            source_name,
                            thematique=thematique,
                        )
                    )
                    next_idx += 1

    return rows


def chunk_markdown_like_notebook(
    doc_markdown: str,
    source_name: str,
    thematique: str = "",
    max_chars: int = 1200,
    overlap: int = 200,
) -> List[dict]:
    text = norm(doc_markdown)
    blocks = parse_blocks_with_fallback(text)
    rows = make_chunks(
        blocks,
        source_name,
        thematique=thematique,
        max_chars=max_chars,
        overlap=overlap,
    )
    return [asdict(row) for row in rows]
