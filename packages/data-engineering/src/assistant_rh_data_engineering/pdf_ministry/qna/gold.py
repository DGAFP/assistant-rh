"""Gold QNA: sections silver (payload metadata.qna) -> chunks Q/R + embeddings.

Reconstruit les SectionBlocks depuis les sections silver puis applique le
chunking QNA legacy (section_blocks_to_chunks). Les lignes produites ont le
même contrat que le gold SECTION_ATOMIC du socle (mêmes colonnes DB), seuls
les rôles et le texte diffèrent (Q_ONLY/QA_COMPOSITE/A_ATOMIC/TABLE, questions
dans le texte). Le seed du hash_id est identique au reste du monorepo:
sha1(source_name|qa_id|role|chunk_index|text[:256]).
"""

from __future__ import annotations

from typing import Any

from ...utils.gold import GoldBundle, GoldRepository, build_chunk_row, build_embedders
from ...utils.helpers import utc_now_iso
from ..config import EmbeddingConfig, GoldConfig
from ..identity import MinistryIdentity
from .engine import QnaEngineConfig, SectionBlock, section_blocks_to_chunks

__all__ = ["QnaGoldBuilder"]


class QnaGoldBuilder:
    def __init__(
        self,
        identity: MinistryIdentity,
        embedding_config: EmbeddingConfig,
        gold_config: GoldConfig,
        engine_config: QnaEngineConfig,
    ):
        self.identity = identity
        self.embedding_config = embedding_config
        self.gold_config = gold_config
        self.engine_config = engine_config
        self._embedders: list[Any] | None = None

    @property
    def embedders(self) -> list[Any]:
        if self._embedders is None:
            self._embedders = build_embedders(self.embedding_config)
        return self._embedders

    @staticmethod
    def _blocks_from_sections(sections: list[dict[str, Any]]) -> tuple[list[SectionBlock], dict[str, str]]:
        blocks: list[SectionBlock] = []
        section_id_by_qa_id: dict[str, str] = {}
        for section in sorted(sections, key=lambda s: int(s.get("section_index") or 0)):
            qna = (section.get("metadata") or {}).get("qna") or {}
            qa_id = str(qna.get("qa_id") or "")
            if not qa_id:
                continue
            blocks.append(
                SectionBlock(
                    qa_id=qa_id,
                    parent_qa_id=qna.get("parent_qa_id"),
                    parent_section_path=None,
                    section_path=str(section.get("heading_path") or section.get("heading") or ""),
                    section_index=int(section.get("section_index") or 0),
                    heading_level=int(section.get("level") or 2),
                    section_title=str(section.get("heading") or ""),
                    pseudo_question=str(qna.get("pseudo_question") or ""),
                    answer=str(qna.get("answer") or ""),
                    source_name=str(qna.get("source_name") or ""),
                    thematique=str(qna.get("thematique") or ""),
                )
            )
            section_id_by_qa_id[qa_id] = str(section.get("section_id") or "")
        return blocks, section_id_by_qa_id

    def build_chunks(self, document: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks, section_id_by_qa_id = self._blocks_from_sections(sections)
        qna_chunks = section_blocks_to_chunks(blocks, self.engine_config)

        chunk_rows: list[dict[str, Any]] = []
        for chunk in qna_chunks:
            # build_chunk_row recompute le hash_id avec le même seed que
            # engine.make_hash_id (contrat d'identité unique, utils/gold.py).
            chunk_rows.append(
                build_chunk_row(
                    document,
                    {
                        "qa_id": chunk.qa_id,
                        "parent_qa_id": chunk.parent_qa_id,
                        "source_name": chunk.source_name,
                        "section_path": chunk.section_path,
                        "role": chunk.role,
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text,
                        "thematique": chunk.thematique,
                        "lang": "fr",
                        "references_juridiques": chunk.references_juridiques or [],
                        "section_id": section_id_by_qa_id.get(chunk.qa_id) or None,
                    },
                    source=self.identity.chunk_source,
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
