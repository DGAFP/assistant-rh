"""Immutable retrieval values, independent of storage and transport."""

from dataclasses import dataclass
from typing import Literal

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue

type Source = Literal["matte", "mso", "mi", "masa", "service_public", "dgafp", "rgrh"]


@dataclass(frozen=True, slots=True)
class SearchRequest:
    source: Source
    mode: Literal["vector", "lexical", "heading"]
    query: str = ""
    embedding: tuple[float, ...] = ()
    embedding_model: Literal["albert", "bge_scaleway"] = "albert"
    limit: int = 20
    probes: int = 10


@dataclass(frozen=True, slots=True)
class RawChunk:
    source: Source
    chunk_id: str
    text: str
    section_id: str | None
    score: float
    rank: int
    metadata: ConfigValues


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    short_id: str | None
    title: str
    url: str
    publisher: str
    markdown: str
    token_count: int
    updated_date: str | None = None


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    doc_id: str
    heading: str
    heading_path: str
    markdown: str
    legal_references: JsonValue
    document: Document | None


@dataclass(frozen=True, slots=True)
class LegalReference:
    number: str
    cid: str
    url: str
    title: str
