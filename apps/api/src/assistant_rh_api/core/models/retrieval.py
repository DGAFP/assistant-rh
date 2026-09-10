"""Immutable retrieval values, independent of storage and transport."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from assistant_rh_api.core.models.configuration import ConfigValues, JsonValue

type Source = Literal["matte", "mso", "mi", "masa", "service_public", "dgafp", "rgrh", "service_public_scw", "dgafp_scw"]


def freeze_metadata(value: ConfigValues) -> ConfigValues:
    """Detach nested metadata from port implementations and callers."""

    def freeze(item: JsonValue) -> JsonValue:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (tuple, list)):
            return tuple(freeze(child) for child in item)
        return item

    return MappingProxyType({key: freeze(item) for key, item in value.items()})


@dataclass(frozen=True, slots=True)
class RetrievalSource:
    key: Source
    name: str
    publisher: str
    headings: bool = False
    r2_pairs: bool = False
    metadata_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata_fields", tuple(self.metadata_fields))


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float
    table_source: str
    metadata: ConfigValues
    section_id: str | None
    embedding_model_used: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


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
