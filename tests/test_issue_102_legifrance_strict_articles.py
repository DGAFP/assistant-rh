from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def stub_bulk_dump(monkeypatch: pytest.MonkeyPatch, tmp_path):
    sync_calls: list[dict] = []
    delete_calls: list[str] = []

    class StubClient:
        def __init__(self, config):
            self.config = config

        def resolve_snapshot(self, raw_dir):
            return SimpleNamespace(
                archive_url="https://example.test/legi.tar.gz",
                archive_name="legi.tar.gz",
                archive_path=tmp_path / "legi.tar.gz",
                extract_dir=tmp_path / "extract",
            )

        def extract_articles(self, snapshot, article_ids):
            return {article_id: SimpleNamespace() for article_id in article_ids if not article_id.startswith("MISSING_")}

        def extract_full_snapshot(self, snapshot):
            return {}

        def delete_local_archive(self, snapshot):
            delete_calls.append(str(snapshot.archive_path))
            return True

    class StubSyncer:
        def __init__(self, config):
            self.config = config

        def sync_medallion_root(self, root, target_env, source_name, delete):
            sync_calls.append({"root": str(root), "target_env": target_env, "source_name": source_name, "delete": delete})
            return {"synced": True}

    class StubObjectStorageConfig:
        @staticmethod
        def from_env():
            return SimpleNamespace()

    import assistant_rh_data_engineering.legifrance.bulk_dump as bulk_dump_module
    import assistant_rh_data_engineering.utils.object_storage as object_storage_module

    monkeypatch.setattr(bulk_dump_module, "LegiBulkDumpClient", StubClient)
    monkeypatch.setattr(object_storage_module, "ScalewayObjectStorageSync", StubSyncer)
    monkeypatch.setattr(object_storage_module, "ObjectStorageConfig", StubObjectStorageConfig)

    return SimpleNamespace(sync_calls=sync_calls, delete_calls=delete_calls)


def write_manifest(tmp_path, article_ids: list[str]):
    path = tmp_path / "articles.json"
    path.write_text(json.dumps({"article_cids": article_ids}), encoding="utf-8")
    return path


def test_article_manifest_is_strict_by_default(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = write_manifest(tmp_path, ["LEGIARTI0001", "MISSING_LEGIARTI0002"])
    monkeypatch.setattr(
        "sys.argv",
        ["legifrance-bulk-dump", "--lake-root", str(tmp_path / "lake"), "--article-ids-json", str(manifest)],
    )

    with pytest.raises(SystemExit) as exc_info:
        legifrance_bulk_dump.main()

    assert "Extraction incomplète" in str(exc_info.value)
    # Non-zero shell exit: SystemExit(<str>) → truthy code, SystemExit(0) → 0
    assert exc_info.value.code
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["reason"] == "incomplete_article_extraction"
    assert payload["extraction_mode"] == "article_ids_json"
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 1
    assert payload["missing_article_count"] == 1
    assert payload["missing_article_ids_sample"] == ["MISSING_LEGIARTI0002"]
    assert payload["strict_articles"] is True


def test_strict_failure_skips_sync_and_local_delete(tmp_path, stub_bulk_dump, monkeypatch) -> None:
    """Fail-fast must run BEFORE Scaleway sync and BEFORE deleting the local archive."""
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = write_manifest(tmp_path, ["LEGIARTI0001", "MISSING_LEGIARTI0002"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "legifrance-bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
            "--sync-object-storage",
            "--delete-remote",
            "--delete-local-archive",
        ],
    )

    with pytest.raises(SystemExit):
        legifrance_bulk_dump.main()

    assert stub_bulk_dump.sync_calls == [], "sync_medallion_root must not run when strict-fail triggers"
    assert stub_bulk_dump.delete_calls == [], "delete_local_archive must not run when strict-fail triggers"


def test_allow_partial_keeps_previous_success_behavior(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = write_manifest(tmp_path, ["LEGIARTI0001", "MISSING_LEGIARTI0002"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "legifrance-bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
            "--allow-partial",
        ],
    )

    assert legifrance_bulk_dump.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 1
    assert payload["missing_article_count"] == 1
    assert payload["missing_article_ids_sample"] == ["MISSING_LEGIARTI0002"]
    assert payload["strict_articles"] is False


def test_strict_succeeds_when_all_articles_are_found(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = write_manifest(tmp_path, ["LEGIARTI0001", "LEGIARTI0002"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "legifrance-bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
        ],
    )

    assert legifrance_bulk_dump.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 2
    assert payload["missing_article_count"] == 0
    assert payload["missing_article_ids_sample"] == []
    assert payload["strict_articles"] is True


def test_duplicate_manifest_entries_reported_as_raw_count(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
    """requested_article_ids must reflect the raw manifest count, not the deduped set."""
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = write_manifest(tmp_path, ["LEGIARTI0001", "LEGIARTI0001", "LEGIARTI0002"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "legifrance-bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
        ],
    )

    assert legifrance_bulk_dump.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_article_ids"] == 3
    assert payload["extracted_xml_count"] == 2


def test_full_snapshot_payload_marks_article_fields_as_not_applicable(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
    """--extract-full-snapshot must NOT emit missing_article_count: 0 (false-green for monitors)."""
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    monkeypatch.setattr(
        "sys.argv",
        [
            "legifrance-bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--extract-full-snapshot",
        ],
    )

    assert legifrance_bulk_dump.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["extraction_mode"] == "full_snapshot"
    assert payload["requested_article_ids"] is None
    assert payload["missing_article_count"] is None
    assert payload["missing_article_ids_sample"] is None
    assert payload["strict_articles"] is None
