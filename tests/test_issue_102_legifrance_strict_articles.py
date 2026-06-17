from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def stub_bulk_dump(monkeypatch: pytest.MonkeyPatch, tmp_path):
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

    import assistant_rh_data_engineering.legifrance.bulk_dump as bulk_dump_module

    monkeypatch.setattr(bulk_dump_module, "LegiBulkDumpClient", StubClient)


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
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "incomplete_article_extraction"
    assert payload["requested_count"] == 2
    assert payload["extracted_xml_count"] == 1
    assert payload["missing_count"] == 1
    assert payload["missing_ids_sample"] == ["MISSING_LEGIARTI0002"]


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
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 1
    assert payload["missing_article_count"] == 1
    assert payload["strict_articles"] is False


def test_explicit_strict_succeeds_when_all_articles_are_found(tmp_path, stub_bulk_dump, monkeypatch, capsys) -> None:
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
            "--strict-articles",
        ],
    )

    assert legifrance_bulk_dump.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 2
    assert payload["missing_article_count"] == 0
    assert payload["strict_articles"] is True
