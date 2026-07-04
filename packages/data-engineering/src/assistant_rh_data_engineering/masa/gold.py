from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..service_public.qna_chunking import split_on_paragraphs
from ..service_public.section_splitter import PAGE_MARKER_RE
from ..utils.gold import GoldBundle, GoldRepository, build_embedders
from ..utils.helpers import utc_now_iso
from ..utils.image_annotation import IMAGE_REF_RE
from .config import CHUNK_SOURCE, EmbeddingConfig, GoldConfig

SECTION_CHUNK_ROLE = "SECTION_ATOMIC"

__all__ = ["MasaGoldBuilder", "GoldBundle", "GoldRepository"]

_PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")
_TABLE_SEPARATOR_CHARS = set("-:| ")

# Divergence MASA (audit du lot réel, 2026-07-04): le corpus est riche en
# supports type slides (formations, webinaires, modes opératoires) dont l'OCR
# produit des chunks sans contenu utile — références d'images seules
# (![img-N.jpeg]), titres de slide sans corps, pages de garde d'annexes
# (54 chunks sur 1031 au premier passage). Un chunk n'est gardé que si son
# payload utile (texte hors images, headings et ponctuation) atteint ce seuil.
# Complémentaire de l'enrichissement VLM (bronze): une image informative
# décrite porte du texte réel et passe le seuil; ne restent filtrées que les
# refs non annotées (échec VLM) et les scories de slides.
MIN_CHUNK_PAYLOAD_CHARS = 25

_HEADING_LINE_RE = re.compile(r"^#{1,6}\s.*$", re.MULTILINE)


def _chunk_payload(text: str) -> str:
    """Texte utile d'un chunk: sans références d'images, sans lignes de
    heading, sans ponctuation ni espaces."""
    stripped = IMAGE_REF_RE.sub("", text)
    stripped = _HEADING_LINE_RE.sub("", stripped)
    return re.sub(r"[\W_]+", "", stripped, flags=re.UNICODE)


def _is_table_block(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    return bool(lines) and all(line.lstrip().startswith("|") for line in lines)


def _is_separator_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and "-" in stripped and set(stripped) <= _TABLE_SEPARATOR_CHARS


_NUMERIC_CELL_RE = re.compile(r"^\d+([.,]\d+)?\s*(h|H|€|%|j|J)?$")


def _looks_like_data_row(row: str) -> bool:
    """Une ligne dont une cellule est purement numérique (durée, montant) est
    une ligne de données: la choisir comme en-tête supprimerait toutes ses
    occurrences du corps du tableau."""
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    return any(_NUMERIC_CELL_RE.match(cell) for cell in cells if cell)


def _table_header_and_body(block: str) -> tuple[list[str], list[str]]:
    """Identifie la ligne d'en-tête d'un tableau OCR et nettoie le corps.

    mistral-ocr coupe les grands tableaux à chaque page en ré-émettant la
    ligne d'en-tête et en plaçant parfois la ligne séparatrice au mauvais
    endroit (audit 2026-07-04). La répétition de l'en-tête est le signal le
    plus fiable; à défaut, la ligne au-dessus d'une séparatrice en tête de
    bloc. Les séparatrices parasites et en-têtes redondants sont retirés.
    """
    lines = [line for line in block.splitlines() if line.strip()]
    rows = [line for line in lines if not _is_separator_row(line)]

    header_row: str | None = None
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.strip()] = counts.get(row.strip(), 0) + 1
    for row in rows[:5]:
        if counts[row.strip()] >= 2 and not _looks_like_data_row(row):
            header_row = row
            break
    if header_row is None:
        separator_indexes = [index for index, line in enumerate(lines) if _is_separator_row(line)]
        if separator_indexes and 1 <= separator_indexes[0] <= 2:
            header_row = lines[separator_indexes[0] - 1]

    if header_row is None:
        return [], rows

    separator = "|" + " --- |" * max(1, header_row.count("|") - 1)
    body = [row for row in rows if row.strip() != header_row.strip()]
    return [header_row, separator], body


def _split_table_block(block: str, max_chars: int) -> list[str]:
    """Découpe un grand tableau markdown aux frontières de lignes, en
    re-portant l'en-tête de colonnes sur chaque tranche.

    Audit 2026-07-04 sur corpus MI réel: les référentiels (tableaux de
    centaines de lignes) coupés à max_chars produisaient des tranches sans
    en-tête, illisibles pour le retrieval.
    """
    header, body = _table_header_and_body(block)

    header_len = sum(len(line) + 1 for line in header)
    slices: list[str] = []
    current: list[str] = list(header)
    current_len = header_len
    for row in body:
        if current_len + len(row) + 1 > max_chars and len(current) > len(header):
            slices.append("\n".join(current))
            current = list(header)
            current_len = header_len
        current.append(row)
        current_len += len(row) + 1
    if len(current) > len(header):
        slices.append("\n".join(current))
    return slices or ([block] if block.strip() else [])


def split_section_markdown(text: str, max_chars: int, overlap: int) -> list[str]:
    """Chunking d'une section: prose via split_on_paragraphs, tableaux
    découpés aux frontières de lignes avec en-tête re-porté; un heading
    orphelin (section qui enchaîne sur un tableau) est fusionné dans le
    chunk suivant plutôt que d'exister en chunk minuscule isolé.

    Les marqueurs <!-- PAGE: n --> (consommés par le silver pour
    page_start/page_end) sont retirés du texte des chunks, et les blocs
    tableau consécutifs — un grand tableau coupé à chaque page par l'OCR —
    sont recousus avant découpe.
    """
    text = "\n".join(line for line in text.splitlines() if not PAGE_MARKER_RE.match(line.strip()))

    def _column_count(block: str) -> int:
        first = next((line for line in block.splitlines() if line.strip()), "")
        return first.count("|")

    blocks: list[tuple[bool, str]] = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        if not paragraph.strip():
            continue
        is_table = _is_table_block(paragraph)
        # Couture des tableaux coupés par page: même nombre de colonnes
        # uniquement — deux tableaux distincts adjacents restent séparés.
        if is_table and blocks and blocks[-1][0] and _column_count(paragraph) == _column_count(blocks[-1][1]):
            blocks[-1] = (True, f"{blocks[-1][1]}\n{paragraph}")
        else:
            blocks.append((is_table, paragraph))

    chunks: list[str] = []
    prose_buffer: list[str] = []

    def flush_prose() -> None:
        if not prose_buffer:
            return
        joined = "\n\n".join(prose_buffer)
        chunks.extend(split_on_paragraphs(joined, max_chars, overlap) or [joined])
        prose_buffer.clear()

    for is_table, block in blocks:
        if is_table and len(block) > max_chars:
            flush_prose()
            chunks.extend(_split_table_block(block, max_chars))
        else:
            prose_buffer.append(block)
    flush_prose()

    first = chunks[0].strip() if chunks else ""
    if len(chunks) >= 2 and first.startswith("#") and "\n" not in first and len(first) < 120:
        chunks[1] = f"{chunks[0]}\n\n{chunks[1]}"
        chunks = chunks[1:]
    return chunks


class MasaGoldBuilder:
    """Gold MASA: sections silver -> chunks + embeddings.

    Contrairement à Service-Public (structure QNA du XML), les documents OCR
    n'ont que le sectionnement heading-based: chaque section indexable est
    découpée en chunks SECTION_ATOMIC. Le champ source vaut CHUNK_SOURCE (MASA)
    — jamais une constante partagée (le hardcode qui a fui dans MATTE).
    Divergence MASA: les chunks sans payload utile sont filtrés
    (MIN_CHUNK_PAYLOAD_CHARS, corpus riche en slides).
    """

    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        self.embedding_config = embedding_config
        self.gold_config = gold_config
        self._embedders: list[Any] | None = None

    @property
    def embedders(self) -> list[Any]:
        if self._embedders is None:
            self._embedders = build_embedders(self.embedding_config)
        return self._embedders

    def build_chunks(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        source_name = Path(str(document.get("storage_path") or "")).name or f"{document['short_id']}.pdf"
        thematique = str((document.get("metadata") or {}).get("theme") or "")

        chunk_rows: list[dict[str, Any]] = []
        for section in sections:
            if section.get("is_indexable") is False:
                continue
            section_markdown = str(section.get("section_markdown") or "").strip()
            if not section_markdown:
                continue

            section_id = str(section.get("section_id") or "")
            section_path = str(section.get("heading_path") or section.get("heading") or "").strip()
            qa_id = hashlib.sha1(f"section:{section_id}".encode("utf-8")).hexdigest()
            texts = split_section_markdown(
                section_markdown,
                self.gold_config.chunk_max_chars,
                self.gold_config.chunk_overlap,
            ) or [section_markdown]
            texts = [text for text in texts if len(_chunk_payload(text)) >= MIN_CHUNK_PAYLOAD_CHARS]

            for index, text in enumerate(texts):
                chunk_rows.append(
                    self._build_chunk_row(
                        document=document,
                        chunk={
                            "qa_id": qa_id,
                            "parent_qa_id": None,
                            "source_name": source_name,
                            "section_path": section_path,
                            "role": SECTION_CHUNK_ROLE,
                            "chunk_index": index,
                            "text": text,
                            "thematique": thematique,
                            "lang": document.get("lang") or "fr",
                            "references_juridiques": section.get("references_juridiques") or [],
                            "section_id": section_id or None,
                        },
                    )
                )

        embedders = self.embedders
        if embedders and chunk_rows:
            texts = [row["text"] for row in chunk_rows]
            for embedder in embedders:
                vectors = embedder.embed_texts(texts)
                for row, vector in zip(chunk_rows, vectors, strict=True):
                    row[embedder.column_name] = vector

        return chunk_rows

    @staticmethod
    def _build_chunk_row(document: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
        row = {
            "qa_id": chunk["qa_id"],
            "parent_qa_id": chunk["parent_qa_id"],
            "source_name": chunk["source_name"],
            "section_path": chunk["section_path"],
            "role": chunk["role"],
            "chunk_index": chunk["chunk_index"],
            "text": chunk["text"],
            "chunk_text": chunk["text"],
            "thematique": chunk["thematique"],
            "lang": chunk["lang"],
            "references_juridiques": chunk.get("references_juridiques") or [],
            "source_document_id": document["doc_id"],
            "section_id": chunk.get("section_id"),
            "short_id": document["short_id"],
            "source": CHUNK_SOURCE,
        }
        seed = "|".join(
            [
                row["source_name"],
                row["qa_id"],
                row["role"],
                str(row["chunk_index"]),
                row["text"][:256],
            ]
        )
        row["hash_id"] = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        return row

    def persist_bundle(self, repository: GoldRepository, silver_bundle: Any) -> GoldBundle:
        chunks = self.build_chunks(silver_bundle.document, silver_bundle.sections)
        short_id = silver_bundle.document["short_id"]
        return GoldBundle(
            document=silver_bundle.document,
            chunks=chunks,
            chunks_path=repository.save_chunks_jsonl(short_id, chunks),
            parquet_path=repository.save_parquet(short_id, chunks) if self.gold_config.export_parquet else None,
            npy_path=repository.save_npy(short_id, chunks, "embedding_m3") if self.gold_config.export_npy else None,
        )

    def save_run_manifest(self, repository: GoldRepository, run_id: str, bundles: list[GoldBundle]) -> None:
        repository.save_manifest(
            {
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "document_count": len(bundles),
                "chunk_count": sum(len(bundle.chunks) for bundle in bundles),
                "documents": [bundle.document["short_id"] for bundle in bundles],
            }
        )
