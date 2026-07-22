"""Interleaving PostgreSQL RÉEL (revue #332, round 2) : le FOR UPDATE de la
revalidation pré-upsert bloque une ingestion concurrente (DELETE par cid)
jusqu'au commit — aucune ligne périmée ne peut survivre.
Gated par R2_PG_TEST_DSN (jamais exécuté en CI sans base)."""

from __future__ import annotations

import os
import threading
import time

import psycopg
import pytest

DSN = os.getenv("R2_PG_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="R2_PG_TEST_DSN non défini (intégration PG réelle)")

TABLE = "r2_race_interleaving_test"


def test_for_update_blocks_concurrent_delete_until_commit() -> None:
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP TABLE IF EXISTS {TABLE}")
        setup.execute(f"CREATE TABLE {TABLE} (chunk_id text PRIMARY KEY, cid text, chunk_text text, index_variant text)")
        setup.execute(f"INSERT INTO {TABLE} VALUES ('C1_0', 'C1', 'texte source', NULL)")
    conn_a = None
    try:
        events: list[tuple[str, float]] = []
        # A = apply R2 : revalidation VERROUILLANTE (même schéma que
        # fetch_article_rows(for_update=True)).
        conn_a = psycopg.connect(DSN)
        cur = conn_a.execute(f"SELECT chunk_id, chunk_text FROM {TABLE} WHERE cid='C1' AND index_variant IS NULL FOR UPDATE")
        assert cur.fetchall()

        def ingestion_concurrente() -> None:
            with psycopg.connect(DSN, autocommit=True) as conn_b:
                events.append(("b_delete_start", time.monotonic()))
                conn_b.execute(f"DELETE FROM {TABLE} WHERE cid='C1'")
                events.append(("b_delete_done", time.monotonic()))

        t = threading.Thread(target=ingestion_concurrente)
        t.start()
        time.sleep(1.5)
        # Tant que A n'a pas commité, le DELETE de B attend sur le verrou.
        assert not any(name == "b_delete_done" for name, _ in events)

        conn_a.execute(f"INSERT INTO {TABLE} VALUES ('C1_r2s', 'C1', 'texte source', 'r2_summary/v+embed-m/x')")
        events.append(("a_commit", time.monotonic()))
        conn_a.commit()
        t.join(timeout=15)
        assert not t.is_alive()

        # CONSTAT PROUVÉ ICI (EvalPlanQual, READ COMMITTED) : le DELETE de B,
        # débloqué après le commit de A, NE VOIT PAS la ligne R2 insérée
        # pendant son attente — elle survit orpheline avec l'ancien texte.
        with psycopg.connect(DSN, autocommit=True) as check:
            rows = check.execute(f"SELECT chunk_id FROM {TABLE}").fetchall()
        assert rows == [("C1_r2s",)], "hypothèse EvalPlanQual non reproduite — revoir la compensation"
        ordre = [name for name, _ in sorted(events, key=lambda e: e[1])]
        assert ordre.index("a_commit") < ordre.index("b_delete_done")

        # La compensation post-commit du job (remove_orphaned_summaries) doit
        # détecter l'article disparu et retirer la ligne R2 orpheline.
        from assistant_rh_data_engineering.jobs.r2_article_summaries import remove_orphaned_summaries
        from assistant_rh_data_engineering.legifrance.summary_rows import source_sha

        with psycopg.connect(DSN) as verify_conn:
            orphaned = remove_orphaned_summaries(
                verify_conn, "public", TABLE,
                cids=["C1"],
                source_shas={"C1": source_sha("texte source")},
                has_index_variant=True,
            )
        assert set(orphaned) == {"C1"}
        with psycopg.connect(DSN, autocommit=True) as check:
            rows = check.execute(f"SELECT chunk_id FROM {TABLE}").fetchall()
        assert rows == []
    finally:
        if conn_a is not None and not conn_a.closed:
            conn_a.close()
        with psycopg.connect(DSN, autocommit=True) as setup:
            setup.execute(f"DROP TABLE IF EXISTS {TABLE}")
