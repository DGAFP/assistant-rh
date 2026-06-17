from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "matte_embedding_tables.json"
SCALEWAY_SCRIPT = REPO_ROOT / "scripts" / "create_scaleway_matte_embeddings_job.sh"
CLI_MAIN_PATH = REPO_ROOT / "apps" / "data-ingestion-cli" / "src" / "assistant_rh_data_ingestion_cli" / "main.py"


def _load_cli_main():
    spec = importlib.util.spec_from_file_location("data_ingestion_cli_main", CLI_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("data_ingestion_cli_main", module)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_matte_embedding_manifest_targets_canonical_columns() -> None:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    tables = payload["tables"]
    assert len(tables) == 1
    spec = tables[0]
    assert spec["table"] == "rag_chunks_matte"
    assert spec["id_column"] == "hash_id"
    assert spec["text_column"] == "chunk_text"

    columns = {item["column"]: item["algorithm"] for item in spec["embeddings"]}
    assert columns == {"embedding_m3": "m3", "embedding_bge_scw": "bge_scaleway"}


def test_cli_resolves_embeddings_matte_to_manifest() -> None:
    cli = _load_cli_main()
    resolved = cli._resolve_command(["embeddings", "matte"])

    assert resolved is not None
    spec, job_args = resolved
    assert spec.module == "assistant_rh_data_engineering.jobs.embeddings_backfill"
    assert job_args == ["--config", "config/matte_embedding_tables.json"]


def test_cli_embeddings_matte_passthrough_preserves_user_config() -> None:
    cli = _load_cli_main()
    resolved = cli._resolve_command(["embeddings", "matte", "--config", "custom/path.json", "--limit", "10"])

    assert resolved is not None
    _, job_args = resolved
    assert job_args == ["--config", "custom/path.json", "--limit", "10"]


def test_scaleway_job_script_targets_matte() -> None:
    content = SCALEWAY_SCRIPT.read_text(encoding="utf-8")

    assert "JOB_NAME:-matte-embeddings-" in content
    assert "args.0=embeddings" in content
    assert "args.1=matte" in content
