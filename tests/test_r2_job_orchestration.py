"""Orchestration du job R2 (revue #332) : rapport --limit honnête et
revalidation pré-upsert (une ingestion concurrente modifie un article pendant
la génération -> ligne ignorée + rapportée, jamais réinsérée)."""

from __future__ import annotations

import argparse
from typing import Any

import pytest
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


def test_explicit_apply_requires_review_confirmation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TEST_R2_DSN", "postgresql://fake")
    args = argparse.Namespace(
        dsn_env="TEST_R2_DSN",
        env_file=str(tmp_path / "absent.env"),
        mode="apply",
        generate=False,
        apply=False,
        reviewed_cache=False,
    )

    with pytest.raises(RuntimeError, match="reviewed-cache"):
        job.run(args)


def test_explicit_apply_never_generates_and_fails_closed_on_cache_miss(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TEST_R2_DSN", "postgresql://fake")
    monkeypatch.setattr(job, "fetch_article_rows", lambda *a, **k: [{"cid": "C1", "chunk_text": "texte un"}])
    monkeypatch.setattr(job, "fetch_existing_variants", lambda *a, **k: {})
    monkeypatch.setattr(job, "_table_has_index_variant", lambda *a, **k: True)
    monkeypatch.setattr(job.psycopg, "connect", lambda dsn, **k: _FakeConn())
    monkeypatch.setattr(job, "summarize_articles", lambda *a, **k: pytest.fail("apply cache-only ne doit jamais appeler le LLM"))
    args = argparse.Namespace(
        dsn_env="TEST_R2_DSN",
        env_file=str(tmp_path / "absent.env"),
        mode="apply",
        generate=False,
        apply=False,
        reviewed_cache=True,
        allow_cache_misses=False,
        sync_object_storage=False,
        target_env="staging",
        cache_source_env="staging",
        model="m-test",
        cache_dir=str(tmp_path / "cache"),
        uid=[],
        uids_file=None,
        schema="public",
        table="rag_chunks_dgafp",
        limit=None,
        out=None,
        max_workers=1,
    )

    with pytest.raises(RuntimeError, match="sans cache revu"):
        job.run(args)


def test_generate_hydrates_staging_and_checkpoints_to_prod_gold(monkeypatch, tmp_path) -> None:
    from assistant_rh_data_engineering.utils import object_storage

    monkeypatch.setenv("TEST_R2_DSN", "postgresql://fake")
    monkeypatch.setattr(job, "fetch_article_rows", lambda *a, **k: [{"cid": "C1", "chunk_text": "texte un"}])
    monkeypatch.setattr(job, "fetch_existing_variants", lambda *a, **k: {})
    monkeypatch.setattr(job, "_table_has_index_variant", lambda *a, **k: True)
    monkeypatch.setattr(job.psycopg, "connect", lambda dsn, **k: _FakeConn())

    class _Syncer:
        instance: "_Syncer | None" = None

        def __init__(self, config: Any) -> None:
            self.downloads: list[str] = []
            self.uploads: list[str] = []
            _Syncer.instance = self

        def medallion_prefix(self, target_env: str, layer: str, source_name: str, suffix: str) -> tuple[str, str]:
            return "gold-bucket", f"{target_env}/{layer}/{source_name}/{suffix}"

        def download_directory(self, bucket: str, prefix: str, destination: Any) -> str:
            self.downloads.append(prefix)
            return f"s3://{bucket}/{prefix}/"

        def upload_object(self, path: Any, bucket: str, key: str) -> Any:
            self.uploads.append(key)
            return type("Uploaded", (), {"uri": f"s3://{bucket}/{key}"})()

        def sync_directory(self, source: Any, bucket: str, prefix: str) -> str:
            return f"s3://{bucket}/{prefix}/"

    monkeypatch.setattr(object_storage.ObjectStorageConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(object_storage, "ScalewayObjectStorageSync", _Syncer)

    def fake_summarize(articles_in, summarizer, cache, *, max_workers, on_result):
        article = articles_in[0]
        checksum = job.source_checksum(article["source_text"])
        cache.put(article["uid"], checksum, {"summary": "résumé métier suffisamment fidèle et détaillé"})
        on_result(SummaryBatchItem(uid=article["uid"], checksum=checksum, status="ok", summary="résumé métier suffisamment fidèle et détaillé"))

    monkeypatch.setattr(job, "summarize_articles", fake_summarize)
    args = argparse.Namespace(
        dsn_env="TEST_R2_DSN",
        env_file=str(tmp_path / "absent.env"),
        mode="generate",
        generate=False,
        apply=False,
        reviewed_cache=False,
        allow_cache_misses=False,
        sync_object_storage=True,
        target_env="prod",
        cache_source_env="staging",
        model="m-test",
        cache_dir=str(tmp_path / "cache"),
        uid=[],
        uids_file=None,
        schema="public",
        table="rag_chunks_dgafp",
        limit=None,
        out=None,
        max_workers=1,
    )

    report = job.run(args)

    syncer = _Syncer.instance
    assert syncer is not None
    assert syncer.downloads[0].startswith("staging/gold/legifrance/r2_article_summaries/")
    assert syncer.downloads[1].startswith("prod/gold/legifrance/r2_article_summaries/")
    assert len(syncer.uploads) == 1
    assert syncer.uploads[0].startswith("prod/gold/legifrance/r2_article_summaries/")
    assert report["generated"] == 1
    assert report["cache_persisted_to"].startswith("s3://gold-bucket/prod/gold/legifrance/")


def test_generate_limit_advances_past_cached_articles(monkeypatch, tmp_path) -> None:
    # generate n'écrit rien en base : sans ce comportement, deux runs
    # successifs à --limit 1 resélectionneraient toujours C1 (cache-hit) et le
    # corpus ne progresserait jamais par lots.
    monkeypatch.setenv("TEST_R2_DSN", "postgresql://fake")
    monkeypatch.setenv("ALBERT_API_KEY", "clef-test")
    articles = [
        {"cid": "C1", "chunk_text": "texte un"},
        {"cid": "C2", "chunk_text": "texte deux"},
    ]
    monkeypatch.setattr(job, "fetch_article_rows", lambda *a, **k: [dict(r) for r in articles])
    monkeypatch.setattr(job, "fetch_existing_variants", lambda *a, **k: {})
    monkeypatch.setattr(job, "_table_has_index_variant", lambda *a, **k: True)
    monkeypatch.setattr(job.psycopg, "connect", lambda dsn, **k: _FakeConn())

    summarizer = job.AlbertArticleSummarizer(model="m-test")
    cache = job.ArticleSummaryCache(tmp_path / "cache", summarizer.name, summarizer.version)
    cache.put("C1", job.source_checksum("texte un"), {"summary": "résumé déjà généré et revu du premier article"})

    seen: list[list[str]] = []

    def fake_summarize(articles_in, summarizer_in, cache_in, *, max_workers, on_result):
        seen.append([a["uid"] for a in articles_in])
        for art in articles_in:
            on_result(SummaryBatchItem(uid=art["uid"], checksum="x", status="ok", summary=f"résumé {art['uid']}"))

    monkeypatch.setattr(job, "summarize_articles", fake_summarize)
    args = argparse.Namespace(
        dsn_env="TEST_R2_DSN",
        env_file=str(tmp_path / "absent.env"),
        mode="generate",
        generate=False,
        apply=False,
        reviewed_cache=False,
        allow_cache_misses=False,
        sync_object_storage=False,
        target_env="staging",
        cache_source_env="staging",
        model="m-test",
        cache_dir=str(tmp_path / "cache"),
        uid=[],
        uids_file=None,
        schema="public",
        table="rag_chunks_dgafp",
        limit=1,
        out=None,
        max_workers=1,
    )

    report = job.run(args)

    assert seen == [["C2"]]
    assert report["selected_for_run"] == 1
    assert report["summaries_missing"] == 2


def test_run_limit_report_and_stale_skip(monkeypatch, tmp_path, capsys) -> None:
    articles = [
        {"cid": "C1", "chunk_text": "texte un"},
        {"cid": "C2", "chunk_text": "texte deux"},
        {"cid": "C3", "chunk_text": "texte trois"},
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
        return [{"cid": c, "chunk_text": current_after[c]} for c in uids if c in current_after]

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
