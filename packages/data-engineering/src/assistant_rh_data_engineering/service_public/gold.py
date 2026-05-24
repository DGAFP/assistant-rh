from __future__ import annotations

import hashlib
from typing import Any

from ..utils.gold import GoldBundle, GoldRepository, build_embedders, match_section_id
from ..utils.helpers import utc_now_iso
from .config import EmbeddingConfig, GoldConfig
from .qna_chunking import chunk_markdown_like_notebook


class ServicePublicGoldBuilder:
    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        self.embedding_config = embedding_config
        self.gold_config = gold_config

    def build_chunks(
        self,
        document: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        source_name = f"{document['short_id']}.xml"
        thematique = document["metadata"].get("theme") or ""
        chunks = chunk_markdown_like_notebook(
            doc_markdown=document["doc_markdown"],
            source_name=source_name,
            thematique=thematique,
        )

        chunk_rows: list[dict[str, Any]] = []
        for chunk in chunks:
            section_id = match_section_id(
                chunk.get("section_path", ""),
                sections,
                allow_heading_fallback=True,
                allow_suffix_fallback=True,
            )
            matched_section = next(
                (
                    section
                    for section in sections
                    if section["section_id"] == section_id
                ),
                None,
            )
            chunk_rows.append(
                {
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
                    "references_juridiques": (matched_section or {}).get(
                        "references_juridiques",
                        [],
                    ),
                    "source_document_id": document["doc_id"],
                    "section_id": section_id,
                    "short_id": document["short_id"],
                    "source": "SERVICE PUBLIC",
                }
            )
            seed = "|".join(
                [
                    chunk["source_name"],
                    chunk["qa_id"],
                    chunk["role"],
                    str(chunk["chunk_index"]),
                    chunk["text"][:256],
                ]
            )
            chunk_rows[-1]["hash_id"] = hashlib.sha1(seed.encode("utf-8")).hexdigest()

        embedders = build_embedders(self.embedding_config)
        if embedders and chunk_rows:
            texts = [row["text"] for row in chunk_rows]
            for embedder in embedders:
                vectors = embedder.embed_texts(texts)
                for row, vector in zip(chunk_rows, vectors, strict=True):
                    row[embedder.column_name] = vector

        return chunk_rows

    def persist_bundles(
        self,
        repository: GoldRepository,
        silver_bundles: list[Any],
    ) -> list[GoldBundle]:
        bundles: list[GoldBundle] = []
        for silver_bundle in silver_bundles:
            chunks = self.build_chunks(silver_bundle.document, silver_bundle.sections)
            short_id = silver_bundle.document["short_id"]
            chunks_path = repository.save_chunks_jsonl(short_id, chunks)
            parquet_path = (
                repository.save_parquet(short_id, chunks)
                if self.gold_config.export_parquet
                else None
            )
            npy_path = (
                repository.save_npy(short_id, chunks, "embedding_m3")
                if self.gold_config.export_npy
                else None
            )
            bundles.append(
                GoldBundle(
                    document=silver_bundle.document,
                    chunks=chunks,
                    chunks_path=chunks_path,
                    parquet_path=parquet_path,
                    npy_path=npy_path,
                )
            )

        repository.save_manifest(
            {
                "run_id": repository.root.name + "_" + utc_now_iso().replace(":", "").replace(".", ""),
                "document_count": len(bundles),
                "chunk_count": sum(len(bundle.chunks) for bundle in bundles),
                "documents": [bundle.document["short_id"] for bundle in bundles],
            }
        )
        return bundles
