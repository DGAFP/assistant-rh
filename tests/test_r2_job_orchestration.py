"""Orchestration du job R2 (revue #332) : rapport --limit honnête et
revalidation pré-upsert (une ingestion concurrente modifie un article pendant
la génération -> ligne ignorée + rapportée, jamais réinsérée)."""

from __future__ import annotations

import argparse
from typing import Any

from assistant_rh_data_engineering.jobs import r2_article_summaries as job
from assistant_rh_data_engineering.utils.article_summary import SummaryBatchItem


class _FakeConn:
    read_only = False

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def commit(self) -> None:
        pass

    def execute(self, query: Any, params: Any = None) -> Any:
        # Seul remove_orphaned_summaries exécute du SQL direct dans ce test :
        # l'article C1 est toujours présent et intact au snapshot post-commit.
        class _Res:
            @staticmethod
            def fetchall() -> list[tuple[str, str]]:
                return [("C1", "texte un")]

        return _Res()


def test_run_limit_report_and_stale_skip(monkeypatch, tmp_path, capsys) -> None:
    articles = [
        {"cid": "C1", "chunk_id": "C1_0", "chunk_text": "texte un"},
        {"cid": "C2", "chunk_id": "C2_0", "chunk_text": "texte deux"},
        {"cid": "C3", "chunk_id": "C3_0", "chunk_text": "texte trois"},
    ]
    # État de la base au moment de l'APPLY : C2 modifié par une ingestion
    # concurrente pendant la génération, C1 intact.
    current_after = {"C1": "texte un", "C2": "texte deux MODIFIÉ ENTRETEMPS"}

    lock_calls = {"n": 0}
    events: list[str] = []

    def fake_fetch(conn, schema, table, *, uids=None, has_index_variant, for_update=False):
        if uids is not None and for_update:
            lock_calls["n"] += 1
            events.append("fetch_for_update")
        if uids is None:
            return [dict(r) for r in articles]
        return [{"cid": c, "chunk_id": f"{c}_0", "chunk_text": current_after[c]} for c in uids if c in current_after]

    monkeypatch.setenv("TEST_R2_DSN", "postgresql://fake")
    monkeypatch.setenv("ALBERT_API_KEY", "clef-test")
    monkeypatch.setenv("ALBERT_EMBED_MODEL", "emb-test")
    monkeypatch.setattr(job, "fetch_article_rows", fake_fetch)
    monkeypatch.setattr(job, "fetch_existing_variants", lambda *a, **k: {})
    monkeypatch.setattr(job, "_table_has_index_variant", lambda *a, **k: True)
    monkeypatch.setattr(job.psycopg, "connect", lambda dsn, **k: _FakeConn())

    def fake_summarize(articles_in, summarizer, cache, *, max_workers, on_result):
        for art in articles_in:
            on_result(SummaryBatchItem(uid=art["uid"], checksum="x", status="ok", summary=f"résumé {art['uid']}"))

    monkeypatch.setattr(job, "summarize_articles", fake_summarize)

    class _FakeEmbedder:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    import assistant_rh_data_engineering.utils.gold as gold_mod

    monkeypatch.setattr(gold_mod, "AlbertApiEmbedder", _FakeEmbedder)

    upserted: list[dict[str, Any]] = []

    class _FakeWriter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def ensure_legacy_target_table(self) -> None:
            events.append("ensure_ddl")

        def upsert_legacy_chunks(self, rows: list[dict], conn: Any = None) -> int:
            upserted.extend(rows)
            return len(rows)

    monkeypatch.setattr(job, "LegifranceDbWriter", _FakeWriter)

    args = argparse.Namespace(
        dsn_env="TEST_R2_DSN",
        env_file=str(tmp_path / "absent.env"),
        model="m-test",
        cache_dir=str(tmp_path / "cache"),
        uid=[],
        uids_file=None,
        schema="public",
        table="rag_chunks_dgafp",
        limit=2,
        generate=True,
        apply=True,
        out=None,
        max_workers=1,
    )
    report = job.run(args)

    # Rapport --limit honnête : 3 manquants au total, 2 sélectionnés, 0 à jour.
    assert report["summaries_missing"] == 3
    assert report["selected_for_run"] == 2
    assert report["summaries_up_to_date"] == 0
    # Revalidation : C2 (modifié) ignoré + rapporté ; seul C1 upserté,
    # avec l'embed_model dans sa clé de fraîcheur.
    assert report["applied"] == 1
    assert report["stale_skipped"] == 1
    assert "modifié" in report["stale_detail"]["C2"]
    assert [r["chunk_id"] for r in upserted] == ["C1_r2s"]
    assert "+embed-emb-test/" in upserted[0]["index_variant"]
    # La revalidation pré-upsert a bien verrouillé (FOR UPDATE), et la
    # vérification post-commit n'a trouvé aucun orphelin (C1 intact).
    assert lock_calls["n"] == 1
    assert report["orphans_removed"] == 0
    # Le DDL (ensure) passe par sa propre connexion : il DOIT précéder la
    # transaction FOR UPDATE, sinon son ALTER attend derrière les verrous du
    # job lui-même (auto-deadlock du 23/07 qui a gelé le retrieval staging).
    assert events == ["ensure_ddl", "fetch_for_update"]


def test_select_canonical_article_rows_ignores_tail_chunks_and_reports_missing_zero() -> None:
    rows = [
        {"cid": "C1", "chunk_id": "C1_0", "chunk_text": "texte canonique"},
        {"cid": "C1", "chunk_id": "C1_1", "chunk_text": "fragment final"},
        {"cid": "C2", "chunk_id": "C2_1", "chunk_text": "fragment sans canonique"},
    ]

    canonical, missing = job._select_canonical_article_rows(rows)

    assert canonical == [rows[0]]
    assert missing == {"C2": ["C2_1"]}


def test_multichunk_canonical_selection_keeps_freshness_idempotent() -> None:
    version = "r2s-test"
    rows = [
        {"cid": "C1", "chunk_id": "C1_0", "chunk_text": "texte canonique"},
        {"cid": "C1", "chunk_id": "C1_1", "chunk_text": "fragment final"},
    ]
    canonical, missing = job._select_canonical_article_rows(rows)
    existing = {"C1": job.build_index_variant(version, "texte canonique", embed_model="emb-test")}

    first_plan = job.plan_missing_summaries(canonical, existing, version, embed_model="emb-test")
    second_plan = job.plan_missing_summaries(canonical, existing, version, embed_model="emb-test")

    assert missing == {}
    assert first_plan == []
    assert second_plan == []
