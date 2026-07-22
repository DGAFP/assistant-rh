"""Interleaving PostgreSQL RÉEL (revue #332, rounds 2-3) : quel que soit
l'ordre d'exécution entre l'apply R2 et une ingestion concurrente, AUCUNE
ligne-résumé périmée ne survit. Trois mécanismes se combinent :
- verrou FOR UPDATE de la revalidation pré-upsert (apply),
- vérification post-commit ELLE-MÊME verrouillante (bloque derrière un
  DELETE en cours au lieu de lire un snapshot d'avant-suppression),
- 2e passe à snapshot frais côté ingestion (READ COMMITTED : un statement
  séparé voit la ligne R2 insérée pendant l'attente du premier DELETE).
Le test N'ATTEND PAS la fin du deleter avant la compensation (la fenêtre
que le round 3 a montrée masquée par un join prématuré).
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


def test_no_stale_summary_survives_any_interleaving() -> None:
    from assistant_rh_data_engineering.jobs.r2_article_summaries import remove_orphaned_summaries
    from assistant_rh_data_engineering.legifrance.summary_rows import source_sha

    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP TABLE IF EXISTS {TABLE}")
        setup.execute(f"CREATE TABLE {TABLE} (chunk_id text PRIMARY KEY, cid text, chunk_text text, index_variant text)")
        setup.execute(f"INSERT INTO {TABLE} VALUES ('C1_0', 'C1', 'texte source', NULL)")
    conn_a = None
    try:
        events: list[tuple[str, float]] = []
        # A = apply R2 : revalidation VERROUILLANTE puis upsert.
        conn_a = psycopg.connect(DSN)
        cur = conn_a.execute(f"SELECT chunk_id, chunk_text FROM {TABLE} WHERE cid='C1' AND index_variant IS NULL FOR UPDATE")
        assert cur.fetchall()

        def ingestion_concurrente() -> None:
            # Réplique _ingest_bundle_tx post-round-3 : purge par cid PUIS
            # 2e passe ciblant les lignes R2 (statement séparé = snapshot
            # frais qui VOIT une ligne insérée pendant l'attente du premier).
            with psycopg.connect(DSN) as conn_b:
                events.append(("b_start", time.monotonic()))
                conn_b.execute(f"DELETE FROM {TABLE} WHERE cid='C1'")
                conn_b.execute(f"DELETE FROM {TABLE} WHERE cid='C1' AND index_variant IS NOT NULL")
                conn_b.commit()
                events.append(("b_done", time.monotonic()))

        t = threading.Thread(target=ingestion_concurrente)
        t.start()
        time.sleep(1.0)
        assert not any(name == "b_done" for name, _ in events)  # bloqué derrière le verrou de A

        conn_a.execute(f"INSERT INTO {TABLE} VALUES ('C1_r2s', 'C1', 'texte source', 'r2_summary/v+embed-m/x')")
        events.append(("a_commit", time.monotonic()))
        conn_a.commit()

        # FENÊTRE DU ROUND 3 : compensation lancée SANS attendre la fin du
        # deleter. Grâce au FOR UPDATE de la vérification, elle bloque
        # derrière ses verrous (ou gagne la course — les deux ordres doivent
        # converger vers zéro ligne périmée).
        with psycopg.connect(DSN) as verify_conn:
            orphaned = remove_orphaned_summaries(
                verify_conn, "public", TABLE,
                cids=["C1"],
                source_shas={"C1": source_sha("texte source")},
                has_index_variant=True,
            )
        t.join(timeout=20)
        assert not t.is_alive()

        # Invariant final, quel que soit l'ordre : plus AUCUNE ligne (article
        # supprimé par l'ingestion, ligne R2 éliminée par la 2e passe OU par
        # la compensation — jamais d'orpheline).
        with psycopg.connect(DSN, autocommit=True) as check:
            rows = check.execute(f"SELECT chunk_id FROM {TABLE}").fetchall()
        assert rows == []
        assert orphaned == {} or set(orphaned) == {"C1"}
        ordre = [name for name, _ in sorted(events, key=lambda e: e[1])]
        assert ordre.index("a_commit") < ordre.index("b_done")
    finally:
        if conn_a is not None and not conn_a.closed:
            conn_a.close()
        with psycopg.connect(DSN, autocommit=True) as setup:
            setup.execute(f"DROP TABLE IF EXISTS {TABLE}")
