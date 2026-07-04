from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..service_public.qna_chunking import split_on_paragraphs
from ..utils.gold import GoldBundle, GoldRepository, build_embedders
from ..utils.helpers import utc_now_iso
from .config import CHUNK_SOURCE, EmbeddingConfig, GoldConfig

SECTION_CHUNK_ROLE = "SECTION_ATOMIC"

__all__ = ["MiGoldBuilder", "GoldBundle", "GoldRepository"]


class MiGoldBuilder:
    """Gold MI: sections silver -> chunks + embeddings.

    Contrairement à Service-Public (structure QNA du XML), les documents OCR
    n'ont que le sectionnement heading-based: chaque section indexable est
    découpée en chunks SECTION_ATOMIC. Le champ source vaut CHUNK_SOURCE (MI)
    — jamais une constante partagée (le hardcode qui a fui dans MATTE).
    """

    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        self.embedding_config = embedding_config
        self.gold_config = gold_config

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
            texts = split_on_paragraphs(
                section_markdown,
                self.gold_config.chunk_max_chars,
                self.gold_config.chunk_overlap,
            ) or [section_markdown]

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

        embedders = build_embedders(self.embedding_config)
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
