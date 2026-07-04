from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.convert import ensure_pdf
from ..utils.grist import ManifestRow
from ..utils.helpers import ensure_dir, sha256_file, utc_now_iso, write_json
from ..utils.ocr import OcrProvider, OcrResult
from ..utils.pdf_store import PdfSourceStore
from .config import MINISTERE


@dataclass
class MiBronzeAsset:
    """Un document MI prêt pour le silver: fichier source + OCR (cache ou live)."""

    row: ManifestRow
    sha256: str
    source_path: Path
    ocr: OcrResult
    ocr_from_cache: bool


class MiBronzeRepository:
    def __init__(self, bronze_dir: Path):
        self.root = ensure_dir(bronze_dir)
        self.downloads_dir = ensure_dir(self.root / "downloads")
        self.ocr_dir = ensure_dir(self.root / "ocr")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def save_ocr_markdown(self, short_id: str, markdown: str) -> Path:
        path = self.ocr_dir / f"{short_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def save_manifest_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> Path:
        path = self.manifest_dir / f"manifest_{run_id}.json"
        write_json(path, snapshot)
        return path


class MiBronzeFetcher:
    """Bronze MI: dropzone -> sha256 du fichier d'origine -> PDF -> OCR.

    Les non-PDF (.doc/.docx/.xls/.xlsx) sont convertis via LibreOffice avant
    OCR; le cache OCR (bucket bronze) reste indexé par le sha256 du fichier
    d'origine, donc un re-run ne repaye jamais l'OCR ni la conversion.
    """

    def __init__(
        self,
        store: PdfSourceStore,
        ocr_provider: OcrProvider,
        repository: MiBronzeRepository,
        *,
        target_env: str,
        force_reocr: bool = False,
    ):
        self.store = store
        self.ocr_provider = ocr_provider
        self.repository = repository
        self.target_env = target_env
        self.force_reocr = force_reocr

    def download_and_hash(self, row: ManifestRow) -> tuple[Path, str]:
        """Télécharge le fichier dropzone et retourne (chemin local, sha256)."""
        filename = Path(row.cle_bucket).name or f"{row.short_id}.pdf"
        destination = ensure_dir(self.repository.downloads_dir / row.short_id) / filename
        local_path = self.store.fetch_source_pdf(row.cle_bucket, destination)
        return local_path, sha256_file(local_path)

    def fetch_asset(self, row: ManifestRow, source_path: Path, sha256: str) -> MiBronzeAsset:
        """OCR (cache-hit bronze sinon appel provider) du document téléchargé."""
        cached = None
        if not self.force_reocr:
            cached = self.store.get_cached_ocr(
                self.target_env,
                MINISTERE,
                self.ocr_provider.name,
                self.ocr_provider.version,
                sha256,
            )

        if cached is not None:
            ocr_result = cached
            from_cache = True
        else:
            pdf_path = ensure_pdf(source_path, source_path.parent / "converted")
            ocr_result = self.ocr_provider.ocr_pdf(pdf_path.read_bytes(), document_name=pdf_path.name)
            self.store.put_pdf(self.target_env, MINISTERE, sha256, pdf_path)
            self.store.put_ocr(self.target_env, MINISTERE, sha256, ocr_result)
            from_cache = False

        self.repository.save_ocr_markdown(row.short_id, ocr_result.markdown)
        return MiBronzeAsset(
            row=row,
            sha256=sha256,
            source_path=source_path,
            ocr=ocr_result,
            ocr_from_cache=from_cache,
        )

    def snapshot_manifest(self, run_id: str, rows: list[ManifestRow]) -> Path:
        return self.repository.save_manifest_snapshot(
            run_id,
            {
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "corpus": rows[0].corpus if rows else MINISTERE.upper(),
                "rows": [
                    {
                        "record_id": row.record_id,
                        "uid": row.uid,
                        "titre": row.titre,
                        "cle_bucket": row.cle_bucket,
                        "statut": row.statut,
                    }
                    for row in rows
                ],
            },
        )
