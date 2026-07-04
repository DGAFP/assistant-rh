from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .object_storage import ObjectStorageObject, ScalewayObjectStorageSync
from .ocr import OcrResult

# Bucket dropzone: alimenté par la page d'import admin (Phase E), lu par les
# pipelines. Un seul bucket pour staging et prod: la clé (cle_bucket du
# manifest Grist) est l'identité, préfixée par ministère (mi/..., masa/...).
DEFAULT_DROPZONE_BUCKET = "assistant-rh-sources-pdf"

# Racine des sources PDF dans la couche bronze (cache content-addressed).
PDF_SOURCES_ROOT = "pdf_sources"


class PdfStoreError(RuntimeError):
    """Erreur d'accès au bucket dropzone ou au cache bronze."""


@dataclass(frozen=True)
class OcrCacheKeys:
    bucket: str
    json_key: str
    markdown_key: str


class PdfSourceStore:
    """Accès aux PDF sources (dropzone) et au cache bronze content-addressed.

    Layout bronze (décision 2026-07-03):
      {env}/bronze/pdf_sources/{ministere}/pdfs/{sha256}.pdf
      {env}/bronze/pdf_sources/{ministere}/ocr/{provider}/{version}/{sha256}.json
      {env}/bronze/pdf_sources/{ministere}/ocr/{provider}/{version}/{sha256}.md

    Le cache est indexé par sha256 du PDF: un re-run sur un document inchangé
    ne repaye jamais l'OCR, et deux fournisseurs restent comparables.
    """

    def __init__(
        self,
        sync: ScalewayObjectStorageSync,
        *,
        dropzone_bucket: str | None = None,
    ):
        self.sync = sync
        self.dropzone_bucket = dropzone_bucket or os.getenv("SCW_BUCKET_SOURCES_PDF") or DEFAULT_DROPZONE_BUCKET

    # --- Dropzone -----------------------------------------------------------

    def fetch_source_pdf(self, cle_bucket: str, destination: Path) -> Path:
        """Télécharge le PDF référencé par une ligne de manifest (cle_bucket)."""
        key = cle_bucket.strip().lstrip("/")
        if not key:
            raise PdfStoreError("cle_bucket vide")
        obj = ObjectStorageObject(bucket=self.dropzone_bucket, key=key)
        try:
            return self.sync.download_object(obj, destination)
        except subprocess.CalledProcessError as exc:
            raise PdfStoreError(f"PDF introuvable dans la dropzone: {obj.uri}") from exc

    # --- Cache bronze ---------------------------------------------------------

    def _bronze_prefix(self, target_env: str, ministere: str, suffix: str) -> tuple[str, str]:
        return self.sync.medallion_prefix(
            target_env,
            "bronze",
            source_name=f"{PDF_SOURCES_ROOT}/{ministere.strip().lower()}",
            suffix=suffix,
        )

    def pdf_cache_key(self, target_env: str, ministere: str, sha256: str) -> ObjectStorageObject:
        bucket, prefix = self._bronze_prefix(target_env, ministere, "pdfs")
        return ObjectStorageObject(bucket=bucket, key=f"{prefix}/{sha256}.pdf")

    def ocr_cache_keys(
        self,
        target_env: str,
        ministere: str,
        provider: str,
        version: str,
        sha256: str,
    ) -> OcrCacheKeys:
        bucket, prefix = self._bronze_prefix(target_env, ministere, f"ocr/{provider}/{version}")
        return OcrCacheKeys(
            bucket=bucket,
            json_key=f"{prefix}/{sha256}.json",
            markdown_key=f"{prefix}/{sha256}.md",
        )

    def get_cached_ocr(
        self,
        target_env: str,
        ministere: str,
        provider: str,
        version: str,
        sha256: str,
    ) -> OcrResult | None:
        """Retourne le résultat OCR mis en cache, ou None (cache miss)."""
        keys = self.ocr_cache_keys(target_env, ministere, provider, version, sha256)
        obj = ObjectStorageObject(bucket=keys.bucket, key=keys.json_key)
        try:
            payload = json.loads(self.sync.read_text_object(obj))
        except subprocess.CalledProcessError:
            return None
        except json.JSONDecodeError as exc:
            raise PdfStoreError(f"Cache OCR corrompu: {obj.uri}") from exc

        # Mêmes garde-fous de forme que le chemin live (utils/ocr.py): un JSON
        # valide mais mal formé doit échouer explicitement, pas se propager.
        if not isinstance(payload, dict):
            raise PdfStoreError(f"Cache OCR corrompu (forme inattendue): {obj.uri}")
        pages = payload.get("pages") or []
        raw = payload.get("raw") or {}
        if not isinstance(pages, list) or not isinstance(raw, dict):
            raise PdfStoreError(f"Cache OCR corrompu (forme inattendue): {obj.uri}")
        if not pages and isinstance(raw.get("pages"), list):
            # Objets écrits par put_ocr après dédoublonnage: les pages vivent
            # uniquement dans raw, re-triées comme au chemin live.
            try:
                pages = sorted(raw["pages"], key=lambda page: int(page.get("index") or 0))
            except (AttributeError, TypeError, ValueError) as exc:
                raise PdfStoreError(f"Cache OCR corrompu (pages de raw invalides): {obj.uri}") from exc

        return OcrResult(
            provider=str(payload.get("provider") or provider),
            version=str(payload.get("version") or version),
            markdown=str(payload.get("markdown") or ""),
            pages=pages,
            raw=raw,
        )

    def put_ocr(
        self,
        target_env: str,
        ministere: str,
        sha256: str,
        result: OcrResult,
    ) -> OcrCacheKeys:
        """Archive le résultat OCR (JSON complet + markdown seul) dans bronze."""
        keys = self.ocr_cache_keys(target_env, ministere, result.provider, result.version, sha256)
        # result.pages référence les mêmes dicts que result.raw["pages"]
        # (le provider trie sans copier): les sérialiser deux fois doublerait
        # l'objet cache — significatif depuis include_image_base64 (les crops
        # pèsent des dizaines de Mo). On ne persiste que raw; la lecture
        # reconstruit pages (get_cached_ocr).
        raw_pages = result.raw.get("pages") if isinstance(result.raw, dict) else None
        pages_in_raw = (
            isinstance(raw_pages, list)
            and len(raw_pages) == len(result.pages)
            and all(any(page is raw_page for raw_page in raw_pages) for page in result.pages)
        )
        payload: dict[str, Any] = {
            "provider": result.provider,
            "version": result.version,
            "sha256": sha256,
            "markdown": result.markdown,
            "pages": [] if pages_in_raw else result.pages,
            "raw": result.raw,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "ocr.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.sync.upload_object(json_path, keys.bucket, keys.json_key)

            markdown_path = Path(tmp_dir) / "ocr.md"
            markdown_path.write_text(result.markdown, encoding="utf-8")
            self.sync.upload_object(markdown_path, keys.bucket, keys.markdown_key)
        return keys

    def put_pdf(
        self,
        target_env: str,
        ministere: str,
        sha256: str,
        pdf_path: Path,
    ) -> ObjectStorageObject:
        """Archive le PDF source dans bronze (restauration/rejeu sans dropzone)."""
        obj = self.pdf_cache_key(target_env, ministere, sha256)
        self.sync.upload_object(pdf_path, obj.bucket, obj.key)
        return obj

    # --- Cache des annotations d'images (enrichissement VLM) -----------------

    def image_annotations_cache_key(
        self,
        target_env: str,
        ministere: str,
        annotator_name: str,
        annotator_version: str,
        sha256: str,
    ) -> ObjectStorageObject:
        bucket, prefix = self._bronze_prefix(
            target_env,
            ministere,
            f"image_annotations/{annotator_name}/{annotator_version}",
        )
        return ObjectStorageObject(bucket=bucket, key=f"{prefix}/{sha256}.json")

    def get_cached_image_annotations(
        self,
        target_env: str,
        ministere: str,
        annotator_name: str,
        annotator_version: str,
        sha256: str,
    ) -> dict[str, dict[str, str]] | None:
        """Annotations d'images en cache pour un document, ou None (miss)."""
        obj = self.image_annotations_cache_key(target_env, ministere, annotator_name, annotator_version, sha256)
        try:
            payload = json.loads(self.sync.read_text_object(obj))
        except subprocess.CalledProcessError:
            return None
        except json.JSONDecodeError:
            # Auto-guérison: un cache d'annotations illisible est traité comme
            # un miss (ré-annotation puis ré-écriture), pas comme une erreur
            # collante qui mettrait le document en échec à chaque run.
            print(f"[warn] cache d'annotations corrompu, ré-annotation: {obj.uri}")
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("annotations"), dict):
            print(f"[warn] cache d'annotations corrompu (forme inattendue), ré-annotation: {obj.uri}")
            return None
        annotations = payload["annotations"]
        for image_id, annotation in annotations.items():
            if (
                not isinstance(annotation, dict)
                or annotation.get("type_image") not in {"decorative", "informative"}
                or not isinstance(annotation.get("description"), str)
            ):
                print(f"[warn] cache d'annotations corrompu (entrée {image_id!r} invalide), ré-annotation: {obj.uri}")
                return None
        return annotations

    def put_image_annotations(
        self,
        target_env: str,
        ministere: str,
        annotator_name: str,
        annotator_version: str,
        sha256: str,
        annotations: dict[str, dict[str, str]],
    ) -> ObjectStorageObject:
        """Archive les annotations d'images d'un document (idempotence VLM)."""
        obj = self.image_annotations_cache_key(target_env, ministere, annotator_name, annotator_version, sha256)
        payload = {
            "annotator": annotator_name,
            "version": annotator_version,
            "sha256": sha256,
            "annotations": annotations,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "annotations.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.sync.upload_object(json_path, obj.bucket, obj.key)
        return obj
