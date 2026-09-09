"""Compose the served RAG configuration from existing DB and environment sources.

Called on every Streamlit page run, before accepting a question. No process or
session cache: a committed admin edit is visible to the next page/request run.
Previously captured values remain detached, including during streaming.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from types import MappingProxyType

from .admin import get_rag_config
from .config import CHUNK_TABLES, ChunkTable, RAGConfig, SnapshotConfig, freeze_config
from .runtime_config import RuntimeRAGConfig, runtime_config_to_rag_config


def load_chunk_tables(environ: Mapping[str, str] | None = None) -> Mapping[str, ChunkTable]:
    environ = os.environ if environ is None else environ
    tables = dict(CHUNK_TABLES)
    for key, variable in (("service_public_scw", "SERVICE_PUBLIC_COMPARE_TABLE"), ("dgafp_scw", "DGAFP_COMPARE_TABLE")):
        name = environ.get(variable, tables[key].name)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid {variable}")
        tables[key] = replace(tables[key], name=name)
    return MappingProxyType(tables)


def validate_configuration(value: SnapshotConfig) -> None:
    """Reject malformed types/nonfinite numbers without tightening tuning ranges."""
    defaults = type(value)()
    for item in fields(value):
        actual, default = getattr(value, item.name), getattr(defaults, item.name)
        if isinstance(default, SnapshotConfig):
            if not isinstance(actual, type(default)):
                raise ValueError(f"Invalid configuration field: {item.name}")
            validate_configuration(actual)
        elif isinstance(value, RuntimeRAGConfig) and item.name == "v3_tables" and actual is None:
            continue  # Historical mapper treats null tables like an empty list.
        elif isinstance(default, (list, tuple)):
            if not isinstance(actual, (list, tuple)) or any(not isinstance(v, str) for v in actual):
                raise ValueError(f"Invalid configuration field: {item.name}")
        elif isinstance(default, float):
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
                raise ValueError(f"Invalid configuration field: {item.name}")
        elif not isinstance(actual, type(default)) or (isinstance(actual, bool) and not isinstance(default, bool)):
            raise ValueError(f"Invalid configuration field: {item.name}")


@dataclass(frozen=True)
class ApplicationConfiguration:
    runtime: RuntimeRAGConfig
    pipeline: RAGConfig
    chunk_tables: Mapping[str, ChunkTable]


def load_application_configuration() -> ApplicationConfiguration:
    runtime = get_rag_config()
    validate_configuration(runtime)
    pipeline = runtime_config_to_rag_config(runtime)
    validate_configuration(pipeline)
    tables = load_chunk_tables()
    if any(key not in tables for key in pipeline.retrieval.tables):
        raise ValueError("Unknown retrieval table in RAG configuration")
    return ApplicationConfiguration(freeze_config(runtime), freeze_config(pipeline), tables)
