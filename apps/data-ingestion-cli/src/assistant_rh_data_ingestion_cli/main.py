from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class CommandSpec:
    module: str
    description: str
    default_args: tuple[str, ...] = ()
    entrypoint: str = "main"


COMMANDS: dict[tuple[str, str], CommandSpec] = {
    ("service-public", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.service_public_medallion",
        "Run the Service-Public bronze/silver/gold pipeline.",
    ),
    ("service-public", "ingest"): CommandSpec(
        "assistant_rh_data_engineering.jobs.service_public_ingestion",
        "Ingest Service-Public gold artifacts into Postgres.",
        entrypoint="run_cli",
    ),
    ("service-public", "ingestion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.service_public_ingestion",
        "Alias for service-public ingest.",
        entrypoint="run_cli",
    ),
    ("legifrance", "bulk-dump"): CommandSpec(
        "assistant_rh_data_engineering.jobs.legifrance_bulk_dump",
        "Download/extract the LEGI bulk dump into bronze/raw.",
    ),
    ("legifrance", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.legifrance_medallion",
        "Run the Legifrance bronze/silver/gold pipeline.",
    ),
    ("legifrance", "ingest"): CommandSpec(
        "assistant_rh_data_engineering.jobs.legifrance_ingestion",
        "Ingest Legifrance silver/gold artifacts into Postgres.",
    ),
    ("legifrance", "ingestion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.legifrance_ingestion",
        "Alias for legifrance ingest.",
    ),
    ("legifrance", "url-backfill"): CommandSpec(
        "assistant_rh_data_engineering.jobs.legifrance_url_backfill",
        "Backfill Legifrance display URLs from silver artifacts.",
    ),
    ("legifrance", "r2-summaries"): CommandSpec(
        "assistant_rh_data_engineering.jobs.r2_article_summaries",
        "Plan, generate, or apply reviewed R2 legal article summaries.",
    ),
    ("mi", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.pdf_sources_medallion",
        "Run the MI (Intérieur) PDF sources medallion pipeline.",
        ("--ministere", "mi"),
    ),
    ("masa", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.pdf_sources_medallion",
        "Run the MASA (Agriculture et Souveraineté alimentaire) PDF sources medallion pipeline.",
        ("--ministere", "masa"),
    ),
    ("matte", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.pdf_sources_medallion",
        "Run the MATTE PDF sources medallion pipeline (rebuild Phase D).",
        ("--ministere", "matte"),
    ),
    ("mso", "medallion"): CommandSpec(
        "assistant_rh_data_engineering.jobs.pdf_sources_medallion",
        "Run the MSO (Ministères sociaux) PDF sources medallion pipeline (rebuild Phase D).",
        ("--ministere", "mso"),
    ),
    ("embeddings", "backfill"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill DB embeddings from an explicit manifest.",
    ),
    ("embeddings", "service-public"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill Service-Public DB embeddings.",
        ("--config", "config/service_public_embedding_tables.json"),
    ),
    ("embeddings", "legifrance"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill Legifrance DB embeddings.",
        ("--config", "config/legifrance_embedding_tables.json"),
    ),
    ("embeddings", "matte"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill MATTE DB embeddings.",
        ("--config", "config/matte_embedding_tables.json"),
    ),
    ("embeddings", "mi"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill MI DB embeddings.",
        ("--config", "config/mi_embedding_tables.json"),
    ),
    ("embeddings", "masa"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill MASA DB embeddings.",
        ("--config", "config/masa_embedding_tables.json"),
    ),
    ("embeddings", "mso"): CommandSpec(
        "assistant_rh_data_engineering.jobs.embeddings_backfill",
        "Backfill MSO DB embeddings.",
        ("--config", "config/mso_embedding_tables.json"),
    ),
    ("chunks", "backfill-text"): CommandSpec(
        "assistant_rh_data_engineering.jobs.chunk_text_backfill",
        "Backfill chunk_text from text where empty (default rag_chunks_matte).",
    ),
    ("observability", "rag-health"): CommandSpec(
        "assistant_rh_data_engineering.jobs.rag_health_exporter",
        "Expose read-only RAG corpus health metrics for Prometheus/Grafana.",
    ),
}


def _print_help() -> None:
    print("Usage: data-ingestion <domain> <job> [job args]\n")
    print("Commands:")
    for (domain, job), spec in sorted(COMMANDS.items()):
        print(f"  {domain:15} {job:16} {spec.description}")


def _has_option(args: Sequence[str], option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in args)


def _resolve_command(argv: Sequence[str]) -> tuple[CommandSpec, list[str]] | None:
    if len(argv) < 2:
        return None
    key = (argv[0], argv[1])
    spec = COMMANDS.get(key)
    if spec is None:
        return None

    job_args = list(argv[2:])
    if spec.default_args and not _has_option(job_args, spec.default_args[0]):
        job_args = [*spec.default_args, *job_args]
    return spec, job_args


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        _print_help()
        return 0

    resolved = _resolve_command(argv)
    if resolved is None:
        _print_help()
        return 2

    spec, job_args = resolved
    module = importlib.import_module(spec.module)
    job_main: Callable[[], int] = getattr(module, spec.entrypoint)

    previous_argv = sys.argv
    sys.argv = [f"data-ingestion {argv[0]} {argv[1]}", *job_args]
    try:
        result = job_main()
        return 0 if result is None else int(result)
    finally:
        sys.argv = previous_argv


if __name__ == "__main__":
    raise SystemExit(main())
