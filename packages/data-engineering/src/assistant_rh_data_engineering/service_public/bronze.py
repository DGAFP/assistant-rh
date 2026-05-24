from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..utils.helpers import ensure_dir, utc_now_iso, write_json
from .config import BronzeConfig


@dataclass
class BronzeAsset:
    fiche_id: str
    xml_bytes: bytes
    xml_path: Path


class BronzeRepository:
    def __init__(self, bronze_dir: Path):
        self.root = ensure_dir(bronze_dir)
        self.raw_dir = ensure_dir(self.root / "raw")
        self.xml_dir = ensure_dir(self.root / "xml")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def save_zip(
        self,
        zip_bytes: bytes,
        filename: str = "vosdroits-latest.zip",
    ) -> Path:
        target = self.raw_dir / filename
        target.write_bytes(zip_bytes)
        return target

    def save_xml(self, fiche_id: str, xml_bytes: bytes) -> Path:
        target = self.xml_dir / f"{fiche_id}.xml"
        target.write_bytes(xml_bytes)
        return target

    def save_manifest(self, manifest: dict) -> Path:
        target = self.manifest_dir / f"bronze_manifest_{manifest['run_id']}.json"
        write_json(target, manifest)
        return target


class ServicePublicXmlFetcher:
    def __init__(self, config: BronzeConfig):
        self.config = config

    def _http_get(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "assistant-rh/1.0 (+https://www.data.gouv.fr/)",
                "Accept": "*/*",
            },
        )
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            return response.read()

    def _fetch_dataset_metadata(self) -> dict:
        api_url = f"{self.config.dataset_api_root}/datasets/{self.config.dataset_slug}/"
        return json.loads(self._http_get(api_url).decode("utf-8"))

    def _select_zip_url(self, dataset: dict) -> str:
        candidates: list[str] = []
        for resource in dataset.get("resources", []):
            for key in ("url", "latest", "original_url"):
                value = resource.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)

        for url in candidates:
            lowered = url.lower()
            if lowered.endswith("vosdroits-latest.zip") or "/zip/" in lowered:
                return url

        for url in candidates:
            if url.lower().endswith(".zip"):
                return url

        raise RuntimeError(
            "Impossible de trouver une ressource ZIP Service-Public "
            "dans data.gouv.fr."
        )

    def download_zip(self) -> tuple[str, bytes]:
        try:
            dataset = self._fetch_dataset_metadata()
            zip_url = self._select_zip_url(dataset)
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            zip_url = self.config.fallback_zip_url

        return zip_url, self._http_get(zip_url)

    def iter_xml_members(
        self,
        zip_bytes: bytes,
        fiche_ids: Optional[Sequence[str]] = None,
    ) -> Iterable[tuple[str, bytes]]:
        wanted = {fid.upper() for fid in fiche_ids or []}
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            for member in archive.namelist():
                if not member.lower().endswith(".xml"):
                    continue
                fiche_id = Path(member).stem.upper()
                if wanted and fiche_id not in wanted:
                    continue
                yield fiche_id, archive.read(member)

    def fetch_to_repository(
        self,
        repository: BronzeRepository,
        fiche_ids: Optional[Sequence[str]] = None,
    ) -> list[BronzeAsset]:
        zip_url, zip_bytes = self.download_zip()
        zip_path = repository.save_zip(zip_bytes)

        assets: list[BronzeAsset] = []
        for fiche_id, xml_bytes in self.iter_xml_members(zip_bytes, fiche_ids):
            xml_path = repository.save_xml(fiche_id, xml_bytes)
            assets.append(
                BronzeAsset(
                    fiche_id=fiche_id,
                    xml_bytes=xml_bytes,
                    xml_path=xml_path,
                )
            )

        repository.save_manifest(
            {
                "run_id": utc_now_iso().replace(":", "").replace(".", ""),
                "created_at": utc_now_iso(),
                "zip_url": zip_url,
                "zip_path": str(zip_path),
                "xml_count": len(assets),
                "fiche_ids": [asset.fiche_id for asset in assets],
            }
        )
        return assets
