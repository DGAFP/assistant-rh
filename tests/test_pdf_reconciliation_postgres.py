"""Réconciliation PDF sur PostgreSQL synthétique, avec un Grist partagé.

Réutilise la base locale de CI Tests, dans des schémas uniques par test/env.
Aucun chargement de .env, aucune connexion à staging/production.
"""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from assistant_rh_data_engineering.jobs.embeddings_backfill import audit_embedding_coverage, evaluate_coverage_report, pdf_corpus_is_empty
from assistant_rh_data_engineering.utils.db import RagDbWriter
from assistant_rh_data_engineering.utils.grist import REQUIRED_MANIFEST_COLUMNS, GristClient, GristConfig, GristContractError, GristError
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


@pytest.fixture
def pdf_databases():
    dsn = os.getenv("API_SYNTHETIC_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("API_SYNTHETIC_POSTGRES_DSN is not configured")
    params = conninfo_to_dict(dsn)
    if (
        params.get("host") not in {"127.0.0.1", "::1", "localhost"}
        or params.get("dbname") != "assistant_rh_api_test"
        or any(key in params for key in ("hostaddr", "service", "servicefile"))
        or any(os.getenv(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"))
    ):
        raise RuntimeError("PDF tests require the local assistant_rh_api_test database")
    schemas = {env: f"pdf_{env}_{uuid4().hex}" for env in ("staging", "prod")}
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "assistant_rh_api_test"
        for schema in schemas.values():
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield dsn, schemas
    finally:
        with psycopg.connect(dsn) as conn:
            for schema in schemas.values():
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def seed_corpus(dsn, schema, identity):
    """FK restrictives : le writer doit réellement supprimer dans le bon ordre."""
    removed_id, other_id = uuid4(), uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("CREATE TABLE rag_documents (doc_id uuid PRIMARY KEY, short_id text, source text, checksum text)")
        conn.execute("CREATE TABLE rag_sections (section_id uuid PRIMARY KEY, doc_id uuid REFERENCES rag_documents)")
        for table in (identity.chunk_table, "rag_chunks_other"):
            conn.execute(
                sql.SQL("""
                CREATE TABLE {} (chunk_id text PRIMARY KEY, short_id text,
                    source_document_id uuid REFERENCES rag_documents,
                    source_section_id uuid REFERENCES rag_sections,
                    embedding_m3 real[], embedding_bge_scw real[], chunk_text text)
            """).format(sql.Identifier(table))
            )
        conn.execute("""
            CREATE TABLE rag_ingestion_runs (run_id text PRIMARY KEY, ministere text, target_env text,
                started_at text, finished_at text, ocr_provider text, expected_count int,
                ingested_count int, skipped_count int, failed_count int, deleted_count int,
                rejected_count int, details jsonb)
        """)
        for doc_id, source, table in ((removed_id, identity.doc_source, identity.chunk_table), (other_id, "other", "rag_chunks_other")):
            section_id = uuid4()
            conn.execute("INSERT INTO rag_documents VALUES (%s, 'PDF-001', %s, 'checksum')", (doc_id, source))
            conn.execute("INSERT INTO rag_sections VALUES (%s, %s)", (section_id, doc_id))
            conn.execute(
                sql.SQL("INSERT INTO {} VALUES ('chunk', 'PDF-001', %s, %s, ARRAY[1.0], ARRAY[2.0], 'texte')").format(sql.Identifier(table)),
                (doc_id, section_id),
            )
    return removed_id, other_id


def make_pipeline(tmp_path, dsn, schema, ministry, env, records, monkeypatch):
    module = importlib.import_module(f"assistant_rh_data_engineering.{ministry}")
    config = module.PipelineConfig(target_env=env, paths=module.LakePaths(root_dir=tmp_path / env))
    config.embeddings.enable_m3 = config.embeddings.enable_bge_scaleway = False
    config.images.enabled = config.page_vision.enabled = False
    grist = GristClient(GristConfig(base_url="https://grist.invalid", api_key="unused", doc_id="test", table_id="Manifest"))

    def get(url, params=None):
        if url.endswith("/columns"):
            return {"columns": [{"id": name} for name in REQUIRED_MANIFEST_COLUMNS]}
        return {"records": records}

    def writeback(record_id, fields):
        record = next(record for record in records if record["id"] == record_id)
        record["fields"].update(fields)

    monkeypatch.setattr(grist, "_get", get)
    monkeypatch.setattr(grist, "writeback_status", writeback)
    pipeline = module.Pipeline(config, grist_client=grist, store=SimpleNamespace(), ocr_provider=SimpleNamespace(name="unused", version="test"))
    pipeline._db_writer = RagDbWriter(dsn=dsn, schema=schema, chunk_table=pipeline.identity.chunk_table)
    return pipeline


@pytest.mark.parametrize("ministry", ["mi", "masa", "matte", "mso"])
@pytest.mark.parametrize("order", [("staging", "prod"), ("prod", "staging")])
@pytest.mark.parametrize("removal", ["row_deleted", "statut", "statut_ingestion"])
def test_pdf_removal_cascades_in_both_environments(pdf_databases, tmp_path, monkeypatch, ministry, order, removal):
    dsn, schemas = pdf_databases
    fields = {
        "source_corpus": ministry.upper(),
        "uid": "PDF-001",
        "titre_document": "PDF retiré",
        "cle_bucket": f"{ministry}/removed.pdf",
        "abroge": "",
        "statut": "ingere",
        "statut_ingestion": "ok",
        "ingere_prod": True,
        "ingere_staging": True,
    }
    records = [] if removal == "row_deleted" else [{"id": 1, "fields": fields}]
    if records:
        fields[removal] = "a_supprimer"
    pipelines = {env: make_pipeline(tmp_path, dsn, schemas[env], ministry, env, records, monkeypatch) for env in order}
    seeded = {env: seed_corpus(dsn, schemas[env], pipeline.identity) for env, pipeline in pipelines.items()}

    for env in order:
        pipeline = pipelines[env]
        planned = pipeline.run(ingest=True, dry_run=True)
        assert planned["plan"]["delete"] == ["PDF-001"]
        summary = pipeline.run(ingest=True)
        assert summary["deleted_count"] == 1
        assert summary["ingested_count"] == summary["failed_count"] == 0
        assert summary["details"]["PDF-001"]["cascade"] == {"documents": 1, "sections": 1, "chunks": 1}
        # Une connexion neuve prouve la suppression COMMIT, y compris des embeddings.
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schemas[env])))
            assert conn.execute("SELECT doc_id FROM rag_documents").fetchall() == [(seeded[env][1],)]
            assert conn.execute("SELECT doc_id FROM rag_sections").fetchall() == [(seeded[env][1],)]
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(pipeline.identity.chunk_table))).fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM rag_chunks_other").fetchone()[0] == 1
            report = audit_embedding_coverage(
                conn,
                schemas[env],
                [
                    {
                        "table": pipeline.identity.chunk_table,
                        "id_column": "chunk_id",
                        "text_column": "chunk_text",
                        "embeddings": [{"column": name} for name in ("embedding_m3", "embedding_bge_scw")],
                    }
                ],
            )
            assert evaluate_coverage_report(report, coverage_min_pct=100, allow_empty_tables=True) == (0, [])
            assert pdf_corpus_is_empty(conn, schemas[env], ministry) is True
        assert pipeline.run(ingest=True)["deleted_count"] == 0

    if records:
        assert fields["ingere_prod"] is fields["ingere_staging"] is False
        assert fields["statut"] == fields["statut_ingestion"] == "supprime"
    for pipeline in pipelines.values():
        repeated = pipeline.run(ingest=True)
        assert repeated["ingested_count"] == repeated["deleted_count"] == 0


@pytest.mark.parametrize("failure", ["malformed", "unavailable"])
def test_invalid_grist_read_preserves_persisted_corpus(pdf_databases, tmp_path, monkeypatch, failure):
    dsn, schemas = pdf_databases
    pipeline = make_pipeline(tmp_path, dsn, schemas["staging"], "mi", "staging", [], monkeypatch)
    seed_corpus(dsn, schemas["staging"], pipeline.identity)
    real_get = pipeline.grist._get

    def get(url, params=None):
        if url.endswith("/columns"):
            return real_get(url, params)
        if failure == "unavailable":
            raise GristError("Grist unavailable")
        return {}

    monkeypatch.setattr(pipeline.grist, "_get", get)
    with pytest.raises((GristContractError, GristError)):
        pipeline.run(ingest=True)
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schemas["staging"])))
        for table in ("rag_documents", "rag_sections", "rag_chunks_mi", "rag_chunks_other"):
            expected = 2 if table in {"rag_documents", "rag_sections"} else 1
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] == expected


def test_cascade_failure_rolls_back_chunks_and_documents(pdf_databases, tmp_path, monkeypatch):
    dsn, schemas = pdf_databases
    schema = schemas["staging"]
    pipeline = make_pipeline(tmp_path, dsn, schema, "mi", "staging", [], monkeypatch)
    seed_corpus(dsn, schema, pipeline.identity)
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("""
            CREATE FUNCTION reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'simulated section deletion failure'; END $$
        """)
        conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON rag_sections FOR EACH ROW EXECUTE FUNCTION reject_delete()")

    with pytest.raises(psycopg.errors.RaiseException, match="simulated section deletion failure"):
        pipeline.run(ingest=True)

    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        assert conn.execute("SELECT count(*) FROM rag_documents").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM rag_sections").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM rag_chunks_mi").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM rag_ingestion_runs").fetchone()[0] == 0


def test_existing_document_without_chunks_is_not_an_empty_corpus(pdf_databases, tmp_path, monkeypatch):
    dsn, schemas = pdf_databases
    schema = schemas["staging"]
    pipeline = make_pipeline(tmp_path, dsn, schema, "mi", "staging", [], monkeypatch)
    seed_corpus(dsn, schema, pipeline.identity)
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("DELETE FROM {}.rag_chunks_mi").format(sql.Identifier(schema)))
        assert pdf_corpus_is_empty(conn, schema, "mi") is False
