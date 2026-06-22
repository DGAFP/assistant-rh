from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "matte_embedding_tables.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCALEWAY_SCRIPT = SCRIPTS_DIR / "create_scaleway_matte_embeddings_job.sh"
CLI_MAIN_PATH = REPO_ROOT / "apps" / "data-ingestion-cli" / "src" / "assistant_rh_data_ingestion_cli" / "main.py"

_CRON_RE = re.compile(r'cron-schedule\.schedule="([^"]+)"')


def _embeddings_job_schedules() -> dict[str, str]:
    """Map each Scaleway embeddings job script to its declared monthly cron slot."""
    schedules: dict[str, str] = {}
    for script in sorted(SCRIPTS_DIR.glob("create_scaleway_*_embeddings_job.sh")):
        match = _CRON_RE.search(script.read_text(encoding="utf-8"))
        if match:
            schedules[script.name] = match.group(1)
    return schedules


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


def test_matte_embeddings_cron_slot_does_not_collide() -> None:
    schedules = _embeddings_job_schedules()

    # MATTE must be wired with its own monthly cron slot.
    assert "create_scaleway_matte_embeddings_job.sh" in schedules

    # Every embeddings job hits the same Scaleway BGE API, so each must own a
    # distinct slot to keep concurrent load staggered. Two jobs sharing a slot
    # (e.g. MATTE on Service-Public's "40 3 1 * *") would fire simultaneously
    # and contend on the shared rate limit.
    slots = list(schedules.values())
    assert len(slots) == len(set(slots)), f"Colliding embeddings cron slots: {schedules}"


def test_matte_embeddings_job_wired_into_deploy() -> None:
    deploy_script = (SCRIPTS_DIR / "deploy_embeddings_jobs.sh").read_text(encoding="utf-8")

    # The deploy orchestrator must invoke the MATTE job script, otherwise the
    # standard deploy creates only the Service-Public and Legifrance jobs and
    # MATTE is never scheduled on Scaleway.
    assert "create_scaleway_matte_embeddings_job.sh" in deploy_script
