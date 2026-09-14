"""Real PostgreSQL identity swap, rollback, replay and removal; no live services."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from assistant_rh_data_engineering.jobs.legifrance_ingestion import ingest_delta
from assistant_rh_data_engineering.legifrance import LegifrancePipeline, LegifrancePipelineConfig
from assistant_rh_data_engineering.legifrance.config import LakePaths
from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter
from assistant_rh_data_engineering.legifrance.live import LegifranceLiveMaterializer, bronze_payload_from_response
from assistant_rh_data_engineering.legifrance.piste import CodeArticle
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

CID = "JORFARTI000001134093"
VERSION = "LEGIARTI000006211069"
TEXT = "JORFTEXT000000590403"


@pytest.fixture
def database():
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
        raise RuntimeError("CID tests require the local assistant_rh_api_test database")
    schema = "legi_cid_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "assistant_rh_api_test"
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute("""
            CREATE TABLE rag_documents (doc_id uuid PRIMARY KEY, short_id text, source text, checksum text,
                metadata jsonb, title text, doc_markdown text, UNIQUE(source, checksum));
            CREATE UNIQUE INDEX uq_rag_documents_short_id ON rag_documents(short_id);
            CREATE TABLE rag_sections (section_id uuid PRIMARY KEY, doc_id uuid REFERENCES rag_documents,
                section_index integer, section_markdown text);
            CREATE UNIQUE INDEX uq_rag_sections_doc_index ON rag_sections(doc_id, section_index);
        """)
    try:
        yield dsn, schema
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("fail_insert", [False, True])
def test_cid_swap_rollback_replay_and_deletion(database, tmp_path, fail_insert):
    dsn, schema = database
    config = LegifrancePipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"))
    config.embeddings.enable_m3 = config.embeddings.enable_bge_scaleway = False
    config.gold.export_parquet = config.gold.export_npy = False
    pipeline = LegifrancePipeline(config)
    article = CodeArticle(CID, "VIGUEUR", "11", VERSION, (CID, VERSION))
    response = {
        "article": {
            "id": VERSION,
            "cid": CID,
            "num": "11",
            "etat": "VIGUEUR",
            "texte": "Contenu synthétique conservé.",
            "textTitles": [{"cid": TEXT, "titre": "Décret synthétique", "nature": "DECRET"}],
        }
    }
    _, payload = bronze_payload_from_response(article, response)
    legacy = {**payload, "cid": VERSION, "origin": "legi_bulk_raw"}
    asset = pipeline.bronze_builder.persist_article_payload(pipeline.bronze_repo, legacy)
    silver = pipeline.run_silver([asset])[0]
    gold = pipeline.run_gold([silver])[0]
    documents, sections, chunks = [silver.document], list(silver.sections), list(gold.chunks)
    writer = LegifranceDbWriter(dsn=dsn, schema=schema)
    writer.ingest_article_bundle(silver.document, silver.sections, gold.chunks)
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute("UPDATE rag_chunks_dgafp SET embedding_m3=%s::vector", (str([1.0] * 1024),))
        if fail_insert:
            conn.execute(
                sql.SQL("""
                CREATE FUNCTION reject_cid() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.short_id = {} THEN RAISE EXCEPTION 'synthetic insert failure'; END IF; RETURN NEW; END $$;
                CREATE TRIGGER reject_cid BEFORE INSERT ON rag_documents FOR EACH ROW EXECUTE FUNCTION reject_cid();
            """).format(sql.Literal(CID))
            )
    fields = {"source_corpus": "Interministériel/Légifrance", "type_id": "legifrance_texte", "jorftext": TEXT, "statut": "ingere", "abroge": "non"}
    grist = SimpleNamespace(list_records=lambda table: [{"id": 1, "fields": fields}])
    piste = SimpleNamespace(text_articles=lambda *args, **kwargs: [article], get_article=lambda uid: response)
    materializer = LegifranceLiveMaterializer(pipeline)

    summary = ingest_delta(writer, grist, piste, documents, sections, chunks, live_materializer=materializer, writeback_enabled=False)

    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        wanted = VERSION if fail_insert else CID
        assert conn.execute("SELECT short_id FROM rag_documents").fetchall() == [(wanted,)]
        assert conn.execute("SELECT count(*) FROM rag_sections").fetchone()[0] == 1
        assert conn.execute("SELECT DISTINCT cid FROM rag_chunks_dgafp").fetchall() == [(wanted,)]
        if fail_insert:
            assert summary["status"] == "partial"
            assert conn.execute("SELECT bool_and(embedding_m3 IS NOT NULL) FROM rag_chunks_dgafp").fetchone()[0]
            conn.execute("DROP TRIGGER reject_cid ON rag_documents")
        else:
            assert summary["applied"]["identity_migrations"] == 1

    # Restart after Silver sync, including the failed DB-insert case, with a
    # version-only TOC and a targeted run. The real writer must keep coverage.
    piste.text_articles = lambda *args, **kwargs: [CodeArticle(VERSION, "VIGUEUR", "11", VERSION, (VERSION,))]
    replay = ingest_delta(
        writer, grist, piste, documents, sections, chunks, requested={VERSION}, live_materializer=materializer, writeback_enabled=False
    )
    assert replay["status"] == "ok"
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        assert conn.execute("SELECT short_id FROM rag_documents").fetchall() == [(CID,)]
        assert conn.execute("SELECT DISTINCT cid FROM rag_chunks_dgafp").fetchall() == [(CID,)]

    fields["statut"] = "a_supprimer"
    removal = ingest_delta(writer, grist, piste, documents, sections, chunks, writeback_enabled=False)
    assert removal["applied"]["deleted"] == 1
    with psycopg.connect(dsn) as conn:
        for table in ("rag_documents", "rag_sections", "rag_chunks_dgafp"):
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0] == 0
