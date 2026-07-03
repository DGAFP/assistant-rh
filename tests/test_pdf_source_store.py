"""Tests du store PDF (utils/pdf_store.py) — S3 mocké via un faux sync."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from assistant_rh_data_engineering.utils.helpers import sha256_bytes, sha256_file
from assistant_rh_data_engineering.utils.object_storage import (
    ObjectStorageConfig,
    ObjectStorageObject,
    ScalewayObjectStorageSync,
)
from assistant_rh_data_engineering.utils.ocr import OcrResult
from assistant_rh_data_engineering.utils.pdf_store import (
    DEFAULT_DROPZONE_BUCKET,
    PdfSourceStore,
    PdfStoreError,
)


class FakeSync:
    """Faux ScalewayObjectStorageSync: en mémoire, pas d'aws CLI."""

    def __init__(self):
        self.config = ObjectStorageConfig(
            region="fr-par",
            access_key="ak",
            secret_key="sk",
            bucket_bronze="assistant-rh-bronze",
            bucket_silver="assistant-rh-silver",
            bucket_gold="assistant-rh-gold",
            prefix_staging="staging",
            prefix_prod="prod",
        )
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []

    # Signatures identiques à ScalewayObjectStorageSync pour les méthodes utilisées.
    def medallion_prefix(
        self,
        target_env: str,
        layer: str,
        source_name: str = "service_public",
        suffix: str = "",
    ) -> tuple[str, str]:
        return ScalewayObjectStorageSync.medallion_prefix(self, target_env, layer, source_name, suffix)  # type: ignore[arg-type]

    def _bucket_for_layer(self, layer: str) -> str:
        return ScalewayObjectStorageSync._bucket_for_layer(self, layer)  # type: ignore[arg-type]

    def download_object(self, obj: ObjectStorageObject, destination: Path) -> Path:
        self.downloads.append(obj.uri)
        if obj.uri not in self.objects:
            raise subprocess.CalledProcessError(1, ["aws", "s3", "cp", obj.uri])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[obj.uri])
        return destination

    def read_text_object(self, obj: ObjectStorageObject) -> str:
        if obj.uri not in self.objects:
            raise subprocess.CalledProcessError(1, ["aws", "s3", "cp", obj.uri])
        return self.objects[obj.uri].decode("utf-8")

    def upload_object(self, source: Path, bucket: str, key: str) -> ObjectStorageObject:
        obj = ObjectStorageObject(bucket=bucket, key=key)
        self.objects[obj.uri] = source.read_bytes()
        return obj


@pytest.fixture()
def store() -> tuple[PdfSourceStore, FakeSync]:
    sync = FakeSync()
    return PdfSourceStore(sync, dropzone_bucket="assistant-rh-sources-pdf"), sync  # type: ignore[arg-type]


def make_ocr_result(markdown: str = "# Doc") -> OcrResult:
    return OcrResult(
        provider="albert",
        version="ocr-model-1",
        markdown=markdown,
        pages=[{"index": 0, "markdown": markdown, "images": []}],
        raw={"model": "ocr-model-1", "pages": []},
    )


def test_default_dropzone_bucket_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCW_BUCKET_SOURCES_PDF", raising=False)
    assert PdfSourceStore(FakeSync()).dropzone_bucket == DEFAULT_DROPZONE_BUCKET  # type: ignore[arg-type]

    monkeypatch.setenv("SCW_BUCKET_SOURCES_PDF", "custom-bucket")
    assert PdfSourceStore(FakeSync()).dropzone_bucket == "custom-bucket"  # type: ignore[arg-type]


def test_fetch_source_pdf_downloads_from_dropzone(tmp_path: Path, store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, sync = store
    sync.objects["s3://assistant-rh-sources-pdf/mi/MI-0001.pdf"] = b"%PDF-fake"

    destination = tmp_path / "MI-0001.pdf"
    result = pdf_store.fetch_source_pdf(" /mi/MI-0001.pdf ", destination)

    assert result.read_bytes() == b"%PDF-fake"
    assert sync.downloads == ["s3://assistant-rh-sources-pdf/mi/MI-0001.pdf"]


def test_fetch_source_pdf_raises_clean_error_when_missing(tmp_path: Path, store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, _ = store
    with pytest.raises(PdfStoreError, match="introuvable dans la dropzone"):
        pdf_store.fetch_source_pdf("mi/ABSENT.pdf", tmp_path / "x.pdf")

    with pytest.raises(PdfStoreError, match="cle_bucket vide"):
        pdf_store.fetch_source_pdf("  ", tmp_path / "x.pdf")


def test_ocr_cache_roundtrip_is_content_addressed(store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, sync = store
    sha = sha256_bytes(b"%PDF-fake")
    result = make_ocr_result()

    assert pdf_store.get_cached_ocr("staging", "MI", "albert", "ocr-model-1", sha) is None

    keys = pdf_store.put_ocr("staging", "MI", sha, result)

    # Layout bronze: {env}/bronze/pdf_sources/{ministere}/ocr/{provider}/{version}/{sha}.json
    assert keys.bucket == "assistant-rh-bronze"
    assert keys.json_key == f"staging/bronze/pdf_sources/mi/ocr/albert/ocr-model-1/{sha}.json"
    assert keys.markdown_key == f"staging/bronze/pdf_sources/mi/ocr/albert/ocr-model-1/{sha}.md"

    cached = pdf_store.get_cached_ocr("staging", "mi", "albert", "ocr-model-1", sha)
    assert cached is not None
    assert cached.markdown == "# Doc"
    assert cached.provider == "albert"
    assert cached.version == "ocr-model-1"
    assert cached.pages[0]["index"] == 0
    assert cached.raw["model"] == "ocr-model-1"

    # Le markdown seul est aussi archivé (lisible directement).
    markdown_uri = f"s3://assistant-rh-bronze/{keys.markdown_key}"
    assert sync.objects[markdown_uri].decode("utf-8") == "# Doc"


def test_ocr_cache_miss_on_different_provider_version(store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, _ = store
    sha = sha256_bytes(b"%PDF-fake")
    pdf_store.put_ocr("staging", "mi", sha, make_ocr_result())

    assert pdf_store.get_cached_ocr("staging", "mi", "albert", "autre-version", sha) is None
    assert pdf_store.get_cached_ocr("staging", "mi", "lighton", "ocr-model-1", sha) is None
    assert pdf_store.get_cached_ocr("prod", "mi", "albert", "ocr-model-1", sha) is None


def test_corrupted_ocr_cache_raises(store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, sync = store
    sha = sha256_bytes(b"%PDF-fake")
    keys = pdf_store.ocr_cache_keys("staging", "mi", "albert", "ocr-model-1", sha)
    sync.objects[f"s3://{keys.bucket}/{keys.json_key}"] = b"pas du json"

    with pytest.raises(PdfStoreError, match="Cache OCR corrompu"):
        pdf_store.get_cached_ocr("staging", "mi", "albert", "ocr-model-1", sha)


def test_put_pdf_archives_under_content_hash(tmp_path: Path, store: tuple[PdfSourceStore, FakeSync]) -> None:
    pdf_store, sync = store
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    sha = sha256_file(pdf_path)

    obj = pdf_store.put_pdf("prod", "masa", sha, pdf_path)

    assert obj.bucket == "assistant-rh-bronze"
    assert obj.key == f"prod/bronze/pdf_sources/masa/pdfs/{sha}.pdf"
    assert sync.objects[obj.uri] == b"%PDF-fake"


def test_sha_helpers_agree(tmp_path: Path) -> None:
    data = b"contenu identique"
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert sha256_bytes(data) == sha256_file(path)
