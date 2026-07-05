from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .helpers import ensure_dir, write_json, write_jsonl


class BaseBatchEmbedder:
    column_name: str

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class SentenceTransformerEmbedder(BaseBatchEmbedder):
    def __init__(
        self,
        model_name: str,
        column_name: str,
        batch_size: int,
        normalize: bool,
    ):
        self.column_name = column_name
        self.batch_size = batch_size
        self.normalize = normalize
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("sentence-transformers est requis pour générer embedding_m3 localement.") from exc

        self.model = SentenceTransformer(model_name)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
        )
        return vectors.astype(np.float32).tolist()


class AlbertApiEmbedder(BaseBatchEmbedder):
    """Embeddings BGE-M3 via l'API Albert (OpenAI-compatible /embeddings).

    Mêmes vecteurs que le retrieval (embedding_m3, 1024 dims) sans embarquer
    sentence-transformers dans l'image du job — décision pipeline PDF (#246).
    """

    def __init__(self, model_name: str, column_name: str, batch_size: int = 32):
        import os

        self.column_name = column_name
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        self.base_url = (os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        self.api_key = os.getenv("ALBERT_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("ALBERT_API_KEY manquant pour générer embedding_m3 via l'API Albert.")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import requests

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model_name, "input": batch},
                timeout=120,
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            if len(data) != len(batch):
                raise RuntimeError(f"API Albert embeddings: {len(data)} vecteurs pour {len(batch)} textes.")
            vectors.extend(item["embedding"] for item in data)
        return vectors


class ScalewayApiEmbedder(BaseBatchEmbedder):
    def __init__(self, model_name: str, column_name: str):
        import os

        self.column_name = column_name
        self.model_name = model_name
        self.base_url = (os.getenv("SCALEWAY_BASE_URL") or "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1").rstrip("/")
        self.api_key = os.getenv("SCALEWAY_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("SCALEWAY_API_KEY manquant pour générer embedding_bge_scw.")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import requests

        vectors: list[list[float]] = []
        for text in texts:
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model_name, "input": text},
                timeout=30,
            )
            response.raise_for_status()
            vectors.append(response.json()["data"][0]["embedding"])
        return vectors


def build_embedders(embedding_config: Any) -> list[BaseBatchEmbedder]:
    embedders: list[BaseBatchEmbedder] = []
    if embedding_config.enable_m3:
        m3_backend = getattr(embedding_config, "m3_backend", "sentence_transformers")
        if m3_backend == "albert_api":
            embedders.append(
                AlbertApiEmbedder(
                    model_name=embedding_config.m3_model_name,
                    column_name="embedding_m3",
                    batch_size=embedding_config.batch_size,
                )
            )
        elif m3_backend != "sentence_transformers":
            # Échec franc à la construction: le fallback silencieux vers
            # sentence-transformers planterait en fin de run dans les images
            # de job qui ne l'embarquent pas, avec un message trompeur.
            raise RuntimeError(f"m3_backend inconnu: {m3_backend!r} (attendus: albert_api, sentence_transformers)")
        else:
            embedders.append(
                SentenceTransformerEmbedder(
                    model_name=embedding_config.m3_model_name,
                    column_name="embedding_m3",
                    batch_size=embedding_config.batch_size,
                    normalize=embedding_config.normalize,
                )
            )
    if embedding_config.enable_bge_scaleway:
        embedders.append(
            ScalewayApiEmbedder(
                model_name=embedding_config.scaleway_model_name,
                column_name="embedding_bge_scw",
            )
        )
    return embedders


def match_section_id(
    section_path: str,
    sections: list[dict[str, Any]],
    *,
    allow_heading_fallback: bool = False,
    allow_suffix_fallback: bool = False,
) -> Optional[str]:
    if not section_path:
        return None

    exact_matches = [section for section in sections if (section.get("heading_path") or "") == section_path]
    if len(exact_matches) == 1:
        return exact_matches[0]["section_id"]

    if allow_heading_fallback:
        heading = section_path.split(" > ")[-1].strip()
        heading_matches = [section for section in sections if (section.get("heading") or "").strip() == heading]
        if len(heading_matches) == 1:
            return heading_matches[0]["section_id"]

    if allow_suffix_fallback:
        suffix_matches = []
        for section in sections:
            heading_path = section.get("heading_path") or ""
            if heading_path.endswith(section_path) or section_path.endswith(heading_path):
                suffix_matches.append(section)
        if len(suffix_matches) == 1:
            return suffix_matches[0]["section_id"]

    return sections[0]["section_id"] if len(sections) == 1 else None


@dataclass
class GoldBundle:
    document: dict[str, Any]
    chunks: list[dict[str, Any]]
    chunks_path: Path
    parquet_path: Optional[Path]
    npy_path: Optional[Path]


class GoldRepository:
    def __init__(self, gold_dir: Path):
        self.root = ensure_dir(gold_dir)
        self.chunks_dir = ensure_dir(self.root / "chunks")
        self.parquet_dir = ensure_dir(self.root / "parquet")
        self.npy_dir = ensure_dir(self.root / "npy")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def save_chunks_jsonl(self, short_id: str, chunks: list[dict[str, Any]]) -> Path:
        path = self.chunks_dir / f"{short_id}.chunks.jsonl"
        write_jsonl(path, chunks)
        return path

    def save_parquet(
        self,
        short_id: str,
        chunks: list[dict[str, Any]],
    ) -> Optional[Path]:
        path = self.parquet_dir / f"{short_id}.chunks.parquet"
        df = pd.DataFrame(chunks)
        for col in ("embedding_m3", "embedding_bge_scw"):
            if col in df.columns:
                df[col] = df[col].apply(lambda value: (json.dumps(value) if isinstance(value, list) else value))
        try:
            df.to_parquet(path, index=False)
        except (ImportError, ValueError):
            return None
        return path

    def save_npy(
        self,
        short_id: str,
        chunks: list[dict[str, Any]],
        column_name: str,
    ) -> Optional[Path]:
        matrix = [row.get(column_name) for row in chunks if row.get(column_name) is not None]
        if not matrix:
            return None
        path = self.npy_dir / f"{short_id}.{column_name}.npy"
        np.save(path, np.array(matrix, dtype=np.float32))
        return path

    def save_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.manifest_dir / f"gold_manifest_{manifest['run_id']}.json"
        write_json(path, manifest)
        return path


# Rôle des chunks section-atomiques: valeur discriminante de la colonne
# `role` en base, partagée entre tous les corpus qui chunkent par section.
SECTION_CHUNK_ROLE = "SECTION_ATOMIC"


def build_chunk_row(document: dict, chunk: dict, *, source: str) -> dict:
    """Ligne de chunk au contrat DB commun + hash_id.

    Le seed du hash_id (source_name|qa_id|role|chunk_index|text[:256]) est le
    CONTRAT D'IDENTITÉ des chunks, partagé par tous les corpus (Service-Public,
    ministères PDF): il ne doit exister qu'ici — un fork silencieux entre
    copies changerait les hash_id d'un corpus (dédup/upsert incohérents).
    Seule la valeur de `source` varie (jamais une constante partagée: le
    hardcode SERVICE PUBLIC qui avait fui dans MATTE est le bug à éviter).
    """
    import hashlib

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
        "source": source,
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
