"""Configuration lifecycle regressions, without a database or provider calls."""

import importlib.util
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from assistant_rh_rag_pipeline import admin, create_pipeline
from assistant_rh_rag_pipeline import configuration_wiring as wiring
from assistant_rh_rag_pipeline.config import CHUNK_TABLES, RAGConfig, freeze_config
from assistant_rh_rag_pipeline.runtime_config import RuntimeRAGConfig, runtime_config_to_rag_config


def test_startup_loads_validates_and_admin_commit_refreshes_next_request(monkeypatch):
    row = RuntimeRAGConfig().to_dict()
    monkeypatch.setattr(admin, "get_runtime_config", lambda: dict(row))
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    pending = {}

    def execute(sql, args):
        pending.update(json.loads(args[0]))

    cursor.execute.side_effect = execute
    conn.commit.side_effect = lambda: row.update(pending)
    monkeypatch.setattr(admin, "_db_conn", lambda: conn)
    first = wiring.load_application_configuration()
    assert first.pipeline.generation.temperature == 0.0
    assert admin.update_rag_config(v3_temperature=0.7, v3_tables=["service_public"]) == (True, {})
    second = wiring.load_application_configuration()
    assert second.pipeline.generation.temperature == 0.7
    assert second.pipeline.retrieval.tables == ("service_public",)
    assert first.pipeline.generation.temperature == 0.0
    assert first.pipeline.retrieval.tables == ("matte", "service_public", "dgafp", "rgrh")
    assert conn.commit.call_count == 1


def test_pipeline_and_every_stage_receive_detached_immutable_configuration(monkeypatch):
    monkeypatch.setattr("assistant_rh_rag_pipeline.query_processor.get_acronym_dict", lambda: {})
    builder = RAGConfig()
    pipe = create_pipeline(builder, dsn="postgresql://synthetic.invalid/example")
    builder.generation.model = "changed-after-start"
    builder.retrieval.tables.clear()
    assert pipe._generator.config.model == "openweight-large"
    assert pipe._retriever.config.tables == ("matte", "service_public", "dgafp", "rgrh")
    for component, field, value in (
        (pipe.config, "verbose", True),
        (pipe._generator.config, "model", "changed"),
        (pipe._retriever.config, "tables", ()),
        (pipe._aggregator.config, "section_rerank_top_k", 2),
        (pipe._context_builder.config, "token_budget", 100),
        (pipe._query_processor.config, "enable_hyde", True),
        (pipe.config.selector, "enabled", True),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(component, field, value)
        with pytest.raises(FrozenInstanceError):
            delattr(component, field)


def test_snapshot_serialization_and_historical_builder_compatibility():
    config = runtime_config_to_rag_config(RuntimeRAGConfig(v3_context_mode="narrow", v3_tables=[]))
    config.retrieval.initial_top_k = 42  # Historical offline tuning remains supported.
    before = config.to_dict()
    frozen = freeze_config(config)
    assert frozen.to_dict() == before
    assert json.loads(json.dumps(frozen.to_dict())) == json.loads(json.dumps(before))
    serialized = frozen.to_dict()
    serialized["retrieval"]["tables"].clear()
    assert len(frozen.retrieval.tables) == 4


def test_fallback_is_fresh_and_exported_default_is_immutable(monkeypatch):
    monkeypatch.setattr(admin, "get_runtime_config", lambda: {})
    first = admin.get_rag_config()
    first.v3_tables.clear()
    first.v3_temperature = 1.0
    assert admin.get_rag_config().to_dict() == RuntimeRAGConfig().to_dict()
    with pytest.raises(FrozenInstanceError):
        admin.DEFAULT_CONFIG.v3_temperature = 1.0
    with pytest.raises(TypeError):
        CHUNK_TABLES["new"] = CHUNK_TABLES["matte"]
    assert admin.runtime_config_to_rag_config().to_dict() == runtime_config_to_rag_config().to_dict()


@pytest.mark.parametrize(
    "field,value",
    [
        ("v3_temperature", float("nan")),
        ("v3_initial_top_k", True),
        ("v3_enable_selector", "false"),
        ("v3_tables", "matte"),
        ("v3_tables", ["unknown"]),
    ],
)
def test_invalid_persisted_config_rejected_at_startup(monkeypatch, field, value):
    row = {field: value}
    monkeypatch.setattr(admin, "get_runtime_config", lambda: row)
    with pytest.raises(ValueError):
        wiring.load_application_configuration()


def test_environment_tables_resolved_at_startup_and_stable_afterwards(monkeypatch):
    monkeypatch.setattr(admin, "get_runtime_config", lambda: {})
    monkeypatch.setenv("SERVICE_PUBLIC_COMPARE_TABLE", "comparison_one")
    first = wiring.load_application_configuration()
    monkeypatch.setenv("SERVICE_PUBLIC_COMPARE_TABLE", "comparison_two")
    second = wiring.load_application_configuration()
    assert first.chunk_tables["service_public_scw"].name == "comparison_one"
    assert second.chunk_tables["service_public_scw"].name == "comparison_two"
    from assistant_rh_rag_pipeline.retriever import Retriever

    retriever = Retriever(first.pipeline.retrieval, dsn="synthetic", chunk_tables=first.chunk_tables)
    assert retriever.chunk_tables["service_public_scw"].name == "comparison_one"
    assert "comparison_one" in retriever._TABLE_META_COLS
    with pytest.raises(TypeError):
        first.chunk_tables["service_public_scw"] = CHUNK_TABLES["service_public_scw"]


def test_pure_config_module_does_not_read_environment_or_import_db(monkeypatch):
    import builtins
    import sys

    from assistant_rh_rag_pipeline import config

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.endswith("db_helpers"):
            raise AssertionError("DB import from pure config")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr("os.getenv", lambda *args: pytest.fail("environment read at import"))
    spec = importlib.util.spec_from_file_location("isolated_rag_config", Path(config.__file__))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.RAGConfig().retrieval.initial_top_k == 30


def test_feedback_resolution_keeps_environment_table_aliases(monkeypatch):
    from assistant_rh_rag_pipeline.feedback_analyzer import _chunk_table_for_ref, _resolve_chunk_content

    monkeypatch.setenv("SERVICE_PUBLIC_COMPARE_TABLE", "rag_chunks_sp_custom")
    assert _chunk_table_for_ref({"table": "Service-Public (Scaleway)"}, None) == "rag_chunks_sp_custom"
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value = []
    _resolve_chunk_content(engine, [{"chunk_id": "test", "table": "Service-Public (Scaleway)"}])
    assert "FROM rag_chunks_sp_custom" in str(conn.execute.call_args.args[0])


def test_public_config_import_does_not_load_adapters():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from assistant_rh_rag_pipeline.config import RAGConfig
assert 'assistant_rh_rag_pipeline.db_helpers' not in sys.modules
assert 'assistant_rh_rag_pipeline.configuration_wiring' not in sys.modules
assert 'psycopg' not in sys.modules
""",
        ],
        check=True,
    )


def test_historical_null_tables_keep_default_retrieval(monkeypatch):
    monkeypatch.setattr(admin, "get_runtime_config", lambda: {"v3_tables": None})
    snapshot = wiring.load_application_configuration()
    assert snapshot.pipeline.retrieval.tables == tuple(RAGConfig().retrieval.tables)
    assert snapshot.runtime.to_dict()["v3_tables"] is None
