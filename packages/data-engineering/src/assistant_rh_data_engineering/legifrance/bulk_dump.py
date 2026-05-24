from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests

from ..utils.helpers import ensure_dir, write_json
from .helpers import clean_nullable

FULL_SNAPSHOT_PATTERN = re.compile(
    r'href="(?P<name>Freemium_legi_global_(?P<stamp>\d{8}-\d{6})\.tar\.gz)"',
    re.IGNORECASE,
)
DELTA_SNAPSHOT_PATTERN = re.compile(
    r'href="(?P<name>LEGI_(?P<stamp>\d{8}-\d{6})\.tar\.gz)"',
    re.IGNORECASE,
)


@dataclass
class LegiBulkDumpConfig:
    index_url: str = "https://echanges.dila.gouv.fr/OPENDATA/LEGI/"
    archive_url: str | None = None
    timeout_seconds: int = 120
    prefer_full_snapshot: bool = True
    include_delta_updates: bool = True


@dataclass
class LegiBulkSnapshot:
    archive_url: str
    archive_name: str
    archive_path: Path
    extract_dir: Path
    index_path: Path | None = None


class LegiBulkDumpClient:
    def __init__(self, config: LegiBulkDumpConfig):
        self.config = config

    def _parse_archive_matches(
        self,
        index_html: str,
        index_url: str,
        pattern: re.Pattern[str],
    ) -> list[tuple[str, str, str]]:
        dedup: dict[str, tuple[str, str, str]] = {}
        for match in pattern.finditer(index_html):
            name = match.group("name")
            stamp = match.group("stamp")
            dedup[name] = (stamp, name, urljoin(index_url, name))
        return sorted(dedup.values(), key=lambda item: item[0])

    def _select_archive_url(self, index_html: str, index_url: str) -> str:
        pattern = FULL_SNAPSHOT_PATTERN if self.config.prefer_full_snapshot else DELTA_SNAPSHOT_PATTERN
        fallback_pattern = DELTA_SNAPSHOT_PATTERN if self.config.prefer_full_snapshot else FULL_SNAPSHOT_PATTERN
        matches = list(pattern.finditer(index_html)) or list(fallback_pattern.finditer(index_html))
        if not matches:
            raise RuntimeError(f"Impossible de trouver une archive LEGI dans l'index DILA: {index_url}")
        latest = max(matches, key=lambda match: match.group("stamp"))
        return urljoin(index_url, latest.group("name"))

    def _supplementary_delta_archives(
        self,
        index_html: str,
        index_url: str,
        primary_archive_name: str,
    ) -> list[tuple[str, str]]:
        if not self.config.include_delta_updates:
            return []
        full_match = re.match(
            r"Freemium_legi_global_(?P<stamp>\d{8}-\d{6})\.tar\.gz",
            primary_archive_name,
        )
        if not full_match:
            return []
        primary_stamp = full_match.group("stamp")
        deltas = self._parse_archive_matches(index_html, index_url, DELTA_SNAPSHOT_PATTERN)
        candidates = [(name, url) for stamp, name, url in deltas if stamp > primary_stamp]
        candidates.reverse()
        return candidates

    def _ensure_archive_downloaded(self, archives_dir: Path, archive_url: str) -> Path:
        archive_name = archive_url.rstrip("/").split("/")[-1]
        archive_path = archives_dir / archive_name
        if archive_path.exists():
            return archive_path
        with requests.get(
            archive_url,
            stream=True,
            timeout=self.config.timeout_seconds,
        ) as response:
            response.raise_for_status()
            with archive_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        return archive_path

    def resolve_snapshot(self, raw_dir: Path) -> LegiBulkSnapshot:
        bulk_root = ensure_dir(raw_dir / "legi_bulk")
        index_dir = ensure_dir(bulk_root / "index")
        archives_dir = ensure_dir(bulk_root / "archives")
        extracts_dir = ensure_dir(bulk_root / "articles")

        archive_url = clean_nullable(self.config.archive_url)
        index_path: Path | None = None
        if not archive_url:
            response = requests.get(
                self.config.index_url,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            index_html = response.text
            index_path = index_dir / "index.html"
            index_path.write_text(index_html, encoding="utf-8")
            archive_url = self._select_archive_url(index_html, self.config.index_url)

        archive_name = archive_url.rstrip("/").split("/")[-1]
        archive_path = self._ensure_archive_downloaded(archives_dir, archive_url)
        extract_dir = ensure_dir(extracts_dir / archive_name.replace(".tar.gz", ""))
        write_json(
            extract_dir / "snapshot.json",
            {
                "archive_url": archive_url,
                "archive_name": archive_name,
                "archive_path": str(archive_path),
                "index_path": str(index_path) if index_path else None,
            },
        )
        return LegiBulkSnapshot(
            archive_url=archive_url,
            archive_name=archive_name,
            archive_path=archive_path,
            extract_dir=extract_dir,
            index_path=index_path,
        )

    def extract_articles(self, snapshot: LegiBulkSnapshot, article_ids: list[str]) -> dict[str, Path]:
        requested = {article_id.strip() for article_id in article_ids if article_id.strip()}
        found: dict[str, Path] = {}
        archive_hits: dict[str, str] = {}
        archives_scanned: list[str] = []
        if not requested:
            return found

        for article_id in requested:
            existing_path = snapshot.extract_dir / f"{article_id}.xml"
            if existing_path.exists():
                found[article_id] = existing_path
                archive_hits[article_id] = snapshot.archive_name

        missing = requested - set(found)
        if missing:
            archives_scanned.append(snapshot.archive_name)
            with tarfile.open(snapshot.archive_path, "r:gz") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    member_name = Path(member.name).name
                    if not member_name.endswith(".xml"):
                        continue
                    article_id = member_name[:-4]
                    if article_id not in missing:
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    target_path = snapshot.extract_dir / member_name
                    target_path.write_bytes(extracted.read())
                    found[article_id] = target_path
                    archive_hits[article_id] = snapshot.archive_name
                    missing.remove(article_id)
                    if not missing:
                        break

        if missing and snapshot.index_path and self.config.include_delta_updates:
            index_html = snapshot.index_path.read_text(encoding="utf-8")
            archives_dir = snapshot.archive_path.parent
            extracts_root = snapshot.extract_dir.parent
            for archive_name, archive_url in self._supplementary_delta_archives(
                index_html,
                self.config.index_url,
                snapshot.archive_name,
            ):
                if not missing:
                    break
                archives_scanned.append(archive_name)
                archive_path = self._ensure_archive_downloaded(archives_dir, archive_url)
                extract_dir = ensure_dir(extracts_root / archive_name.replace(".tar.gz", ""))
                for article_id in list(missing):
                    existing_path = extract_dir / f"{article_id}.xml"
                    if existing_path.exists():
                        found[article_id] = existing_path
                        archive_hits[article_id] = archive_name
                        missing.remove(article_id)
                if not missing:
                    break
                with tarfile.open(archive_path, "r:gz") as archive:
                    for member in archive:
                        if not member.isfile():
                            continue
                        member_name = Path(member.name).name
                        if not member_name.endswith(".xml"):
                            continue
                        article_id = member_name[:-4]
                        if article_id not in missing:
                            continue
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            continue
                        target_path = extract_dir / member_name
                        target_path.write_bytes(extracted.read())
                        found[article_id] = target_path
                        archive_hits[article_id] = archive_name
                        missing.remove(article_id)
                        if not missing:
                            break

        write_json(
            snapshot.extract_dir / "extracted_articles.json",
            {
                "requested_count": len(requested),
                "found_count": len(found),
                "missing_ids": sorted(missing),
                "archive_hits": archive_hits,
                "archives_scanned": archives_scanned,
            },
        )
        return found

    def extract_full_snapshot(self, snapshot: LegiBulkSnapshot) -> dict[str, Path]:
        extracted: dict[str, Path] = {}
        for path in snapshot.extract_dir.glob("*.xml"):
            extracted[path.stem] = path

        if extracted:
            return extracted

        with tarfile.open(snapshot.archive_path, "r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                member_name = Path(member.name).name
                if not member_name.endswith(".xml"):
                    continue
                extracted_file = archive.extractfile(member)
                if extracted_file is None:
                    continue
                target_path = snapshot.extract_dir / member_name
                target_path.write_bytes(extracted_file.read())
                extracted[target_path.stem] = target_path

        write_json(
            snapshot.extract_dir / "extracted_full_snapshot.json",
            {
                "archive_name": snapshot.archive_name,
                "extracted_count": len(extracted),
            },
        )
        return extracted

    def delete_local_archive(self, snapshot: LegiBulkSnapshot) -> bool:
        if not snapshot.archive_path.exists():
            return False
        snapshot.archive_path.unlink()
        return True
