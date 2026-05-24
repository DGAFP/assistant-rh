from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.helpers import ensure_dir, utc_now_iso, write_json
from .config import BronzeConfig
from .helpers import (
    build_legifrance_article_url,
    clean_nullable,
    count_links,
    make_short_id,
    normalize_article_number,
    normalize_short_id,
)
from .xml_article_parser import parse_article_xml


@dataclass
class BronzeAsset:
    asset_type: str
    short_id: str
    payload: dict[str, Any]
    payload_path: Path | str


class BronzeRepository:
    def __init__(self, bronze_dir: Path):
        self.root = ensure_dir(bronze_dir)
        self.raw_dir = ensure_dir(self.root / "raw")
        self.articles_dir = ensure_dir(self.raw_dir / "articles")
        self.legacy_texts_dir = ensure_dir(self.raw_dir / "legacy_texts")
        self.legacy_text_sources_dir = ensure_dir(self.raw_dir / "legacy_text_sources")
        self.bulk_articles_dir = ensure_dir(self.raw_dir / "legi_bulk" / "articles")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def article_json_paths(self) -> list[Path]:
        return sorted(self.articles_dir.glob("*.json"))

    def legacy_text_json_paths(self) -> list[Path]:
        return sorted(self.legacy_texts_dir.glob("*.json"))

    def legacy_text_source_paths(self) -> list[Path]:
        return sorted(self.legacy_text_sources_dir.glob("*.txt"))

    def bulk_article_xml_paths(self) -> list[Path]:
        return sorted(self.bulk_articles_dir.rglob("*.xml"))

    def save_article_payload(self, short_id: str, payload: dict[str, Any]) -> Path:
        path = self.articles_dir / f"{short_id}.json"
        write_json(path, payload)
        return path

    def save_legacy_text_payload(self, short_id: str, payload: dict[str, Any]) -> Path:
        path = self.legacy_texts_dir / f"{short_id}.json"
        write_json(path, payload)
        return path

    def save_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.manifest_dir / f"bronze_manifest_{manifest['run_id']}.json"
        write_json(path, manifest)
        return path


class LegifranceBronzeBuilder:
    def __init__(self, config: BronzeConfig):
        self.config = config

    @staticmethod
    def _article_sort_key(payload: dict[str, Any]) -> tuple[str, str]:
        return (
            str(payload.get("num_article") or payload.get("short_id") or ""),
            str(payload.get("article_id") or payload.get("cid") or ""),
        )

    def _normalize_article_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        article_id = clean_nullable(payload.get("article_id")) or clean_nullable(payload.get("cid"))
        num_article = clean_nullable(payload.get("num_article")) or clean_nullable(payload.get("number"))
        text = str(payload.get("text") or "").strip()
        if not article_id:
            raise RuntimeError("Article Légifrance sans article_id/cid dans le bronze raw.")
        if not num_article:
            raise RuntimeError(f"Article Légifrance sans num_article pour {article_id}.")
        if not text:
            raise RuntimeError(f"Article Légifrance sans texte pour {article_id}.")

        title = clean_nullable(payload.get("title")) or clean_nullable(payload.get("full_title")) or self.config.legifrance_code_name
        full_title = clean_nullable(payload.get("full_title")) or title
        subtitles = clean_nullable(payload.get("subtitles"))
        full_sections_title = clean_nullable(payload.get("full_sections_title")) or subtitles
        category = str(clean_nullable(payload.get("category")) or "CODE").upper()
        status = str(clean_nullable(payload.get("status")) or "VIGUEUR").upper()
        source_url = build_legifrance_article_url(article_id, category)
        short_id = (
            clean_nullable(payload.get("cid"))
            or clean_nullable(payload.get("article_id"))
            or article_id
        )

        normalized = {
            "asset_type": "article",
            "article_id": article_id,
            "cid": clean_nullable(payload.get("cid")) or article_id,
            "num_article": str(num_article).strip(),
            "num_norm": normalize_article_number(str(num_article)),
            "title": title,
            "full_title": full_title,
            "subtitles": subtitles,
            "full_sections_title": full_sections_title,
            "text": text,
            "source_url": source_url,
            "category": category,
            "status": status,
            "source_name": clean_nullable(payload.get("source_name")),
            "section_parent_cid": clean_nullable(payload.get("section_parent_cid")),
            "section_parent_titre": clean_nullable(payload.get("section_parent_titre")),
            "code_id": clean_nullable(payload.get("code_id")) or (self.config.legifrance_code_id if category == "CODE" else None),
            "state": str(clean_nullable(payload.get("state")) or status).lower(),
            "date_version": clean_nullable(payload.get("date_version")),
            "start_date": clean_nullable(payload.get("start_date")),
            "end_date": clean_nullable(payload.get("end_date")),
            "ministry": clean_nullable(payload.get("ministry")),
            "nota": clean_nullable(payload.get("nota")),
            "lien_citations": payload.get("lien_citations"),
            "lien_citations_count": payload.get("lien_citations_count"),
            "lien_modifications": payload.get("lien_modifications"),
            "lien_modifications_count": payload.get("lien_modifications_count"),
            "lien_concordes": payload.get("lien_concordes"),
            "lien_concordes_count": payload.get("lien_concordes_count"),
            "comporte_liens_sp": bool(payload.get("comporte_liens_sp") or False),
            "origin": clean_nullable(payload.get("origin")) or "raw_article_json",
            "search_source": clean_nullable(payload.get("search_source")) or "bronze_raw",
            "short_id": short_id,
        }

        normalized["lien_citations_count"] = normalized["lien_citations_count"] or count_links(normalized["lien_citations"])
        normalized["lien_modifications_count"] = normalized["lien_modifications_count"] or count_links(normalized["lien_modifications"])
        normalized["lien_concordes_count"] = normalized["lien_concordes_count"] or count_links(normalized["lien_concordes"])
        return normalized

    def _load_article_payloads_from_json(self, repository: BronzeRepository) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for path in repository.article_json_paths():
            payload = self._normalize_article_payload(json.loads(path.read_text(encoding="utf-8")))
            payloads[payload["article_id"]] = payload
        return payloads

    def _select_latest_xml_paths(self, repository: BronzeRepository) -> dict[str, Path]:
        latest_by_article: dict[str, Path] = {}
        for path in repository.bulk_article_xml_paths():
            article_id = path.stem
            current = latest_by_article.get(article_id)
            candidate_key = (path.parent.name, path.stat().st_mtime_ns)
            current_key = ("", -1) if current is None else (current.parent.name, current.stat().st_mtime_ns)
            if candidate_key >= current_key:
                latest_by_article[article_id] = path
        return latest_by_article

    def _load_article_payloads_from_xml(self, repository: BronzeRepository) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for article_id, path in sorted(self._select_latest_xml_paths(repository).items()):
            payload = self._normalize_article_payload(
                {
                    **parse_article_xml(path),
                    "origin": "legi_bulk_raw",
                    "search_source": "legi_bulk_raw",
                }
            )
            repository.save_article_payload(payload["short_id"], payload)
            payloads[article_id] = payload
        return payloads

    def _load_local_article_payloads(self, repository: BronzeRepository) -> list[dict[str, Any]]:
        json_payloads = self._load_article_payloads_from_json(repository)
        if not self.config.prefer_raw_xml:
            return sorted(json_payloads.values(), key=self._article_sort_key)

        xml_payloads = self._load_article_payloads_from_xml(repository)
        payloads = dict(json_payloads)
        payloads.update(xml_payloads)
        return sorted(payloads.values(), key=self._article_sort_key)

    def _load_legacy_text_payloads_from_sources(self, repository: BronzeRepository) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for path in repository.legacy_text_source_paths():
            source_name = path.name
            short_id = make_short_id(source_name)
            payload = {
                "asset_type": "legacy_text",
                "source_name": source_name,
                "source_path": str(path),
                "raw_source_path": str(path),
                "text": path.read_text(encoding="utf-8"),
                "thematique": self.config.default_legacy_thematique,
                "short_id": short_id,
            }
            repository.save_legacy_text_payload(short_id, payload)
            payloads.append(payload)
        return payloads

    def _load_legacy_text_payloads_from_json(self, repository: BronzeRepository) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for path in repository.legacy_text_json_paths():
            payload = json.loads(path.read_text(encoding="utf-8"))
            source_name = str(payload.get("source_name") or path.stem).strip()
            short_id = normalize_short_id(payload.get("short_id"), source_name)
            payloads.append(
                {
                    "asset_type": "legacy_text",
                    "source_name": source_name,
                    "source_path": payload.get("source_path") or payload.get("raw_source_path"),
                    "raw_source_path": payload.get("raw_source_path") or payload.get("source_path"),
                    "text": str(payload.get("text") or "").strip(),
                    "thematique": payload.get("thematique") or self.config.default_legacy_thematique,
                    "short_id": short_id,
                }
            )
        return payloads

    def _load_local_legacy_text_payloads(self, repository: BronzeRepository) -> list[dict[str, Any]]:
        source_payloads = self._load_legacy_text_payloads_from_sources(repository)
        if source_payloads:
            return sorted(source_payloads, key=lambda payload: payload["short_id"])
        return sorted(self._load_legacy_text_payloads_from_json(repository), key=lambda payload: payload["short_id"])

    def _select_latest_remote_xml_objects(self, objects: list[Any]) -> dict[str, Any]:
        latest_by_article: dict[str, Any] = {}
        for obj in objects:
            object_key = str(getattr(obj, "key", "") or "")
            filename = Path(object_key).name
            if not filename.endswith(".xml"):
                continue
            article_id = filename[:-4]
            current = latest_by_article.get(article_id)
            candidate_key = (
                Path(object_key).parent.name,
                str(getattr(obj, "last_modified", "") or ""),
                object_key,
            )
            current_key = (
                "",
                "",
                "",
            ) if current is None else (
                Path(str(getattr(current, "key", "") or "")).parent.name,
                str(getattr(current, "last_modified", "") or ""),
                str(getattr(current, "key", "") or ""),
            )
            if candidate_key >= current_key:
                latest_by_article[article_id] = obj
        return latest_by_article

    def _load_article_payloads_from_remote_xml(
        self,
        object_storage: Any,
        target_env: str,
    ) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        objects = object_storage.list_medallion_objects(
            target_env,
            "bronze",
            "legifrance",
            "raw/legi_bulk/articles",
        )
        latest_objects = self._select_latest_remote_xml_objects(objects)
        if not latest_objects:
            return payloads

        with tempfile.TemporaryDirectory(prefix="legifrance_remote_xml_") as temp_dir:
            temp_root = Path(temp_dir)
            downloaded_by_name = {
                path.name: path
                for path in object_storage.download_objects(list(latest_objects.values()), temp_root)
            }
            for article_id, obj in sorted(latest_objects.items()):
                temp_path = downloaded_by_name.get(Path(str(getattr(obj, "key", "") or "")).name)
                if temp_path is None:
                    raise RuntimeError(
                        "Téléchargement incomplet depuis l'Object Storage pour "
                        f"l'article {article_id}."
                    )
                payload = self._normalize_article_payload(
                    {
                        **parse_article_xml(temp_path),
                        "origin": "legi_bulk_raw",
                        "search_source": "legi_bulk_raw",
                    }
                )
                payloads[article_id] = payload
        return payloads

    def _load_article_payloads_from_remote_json(
        self,
        object_storage: Any,
        target_env: str,
    ) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        objects = object_storage.list_medallion_objects(
            target_env,
            "bronze",
            "legifrance",
            "raw/articles",
        )
        for obj in sorted(objects, key=lambda item: str(getattr(item, "key", "") or "")):
            object_key = str(getattr(obj, "key", "") or "")
            if not object_key.endswith(".json"):
                continue
            payload = self._normalize_article_payload(json.loads(object_storage.read_text_object(obj)))
            payloads[payload["article_id"]] = payload
        return payloads

    def _load_legacy_text_payloads_from_remote_json(
        self,
        object_storage: Any,
        target_env: str,
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        objects = object_storage.list_medallion_objects(
            target_env,
            "bronze",
            "legifrance",
            "raw/legacy_texts",
        )
        for obj in sorted(objects, key=lambda item: str(getattr(item, "key", "") or "")):
            object_key = str(getattr(obj, "key", "") or "")
            if not object_key.endswith(".json"):
                continue
            payload = json.loads(object_storage.read_text_object(obj))
            source_name = str(payload.get("source_name") or Path(object_key).stem).strip()
            short_id = normalize_short_id(payload.get("short_id"), source_name)
            payloads.append(
                {
                    "asset_type": "legacy_text",
                    "source_name": source_name,
                    "source_path": obj.uri if hasattr(obj, "uri") else object_key,
                    "raw_source_path": obj.uri if hasattr(obj, "uri") else object_key,
                    "text": str(payload.get("text") or "").strip(),
                    "thematique": payload.get("thematique") or self.config.default_legacy_thematique,
                    "short_id": short_id,
                }
            )
        return payloads

    def fetch_from_object_storage(
        self,
        repository: BronzeRepository,
        object_storage: Any,
        target_env: str,
    ) -> list[BronzeAsset]:
        if self.config.prefer_raw_xml:
            xml_payloads = self._load_article_payloads_from_remote_xml(object_storage, target_env)
            if xml_payloads:
                article_payloads = sorted(xml_payloads.values(), key=self._article_sort_key)
            else:
                article_payloads = sorted(
                    self._load_article_payloads_from_remote_json(object_storage, target_env).values(),
                    key=self._article_sort_key,
                )
        else:
            article_payloads = sorted(
                self._load_article_payloads_from_remote_json(object_storage, target_env).values(),
                key=self._article_sort_key,
            )
        legacy_text_payloads = sorted(
            self._load_legacy_text_payloads_from_remote_json(object_storage, target_env),
            key=lambda payload: payload["short_id"],
        )

        if not article_payloads and not legacy_text_payloads:
            raise RuntimeError(
                "Aucun artefact exploitable trouvé dans l'Object Storage bronze Légifrance."
            )

        assets: list[BronzeAsset] = []
        for payload in article_payloads:
            assets.append(
                BronzeAsset(
                    asset_type="article",
                    short_id=payload["short_id"],
                    payload=payload,
                    payload_path=f"remote://bronze/legifrance/raw/legi_bulk/articles/{payload.get('article_id')}.xml",
                )
            )

        for payload in legacy_text_payloads:
            assets.append(
                BronzeAsset(
                    asset_type="legacy_text",
                    short_id=payload["short_id"],
                    payload=payload,
                    payload_path=str(payload.get("raw_source_path") or payload.get("source_path") or ""),
                )
            )

        repository.save_manifest(
            {
                "run_id": utc_now_iso().replace(":", "").replace(".", ""),
                "created_at": utc_now_iso(),
                "article_asset_count": sum(asset.asset_type == "article" for asset in assets),
                "legacy_text_asset_count": sum(asset.asset_type == "legacy_text" for asset in assets),
                "raw_articles_dir": "object-storage://bronze/legifrance/raw/articles",
                "raw_bulk_articles_dir": "object-storage://bronze/legifrance/raw/legi_bulk/articles",
                "raw_legacy_text_sources_dir": "object-storage://bronze/legifrance/raw/legacy_texts",
                "assets": [str(asset.payload_path) for asset in assets],
            }
        )
        return assets

    def fetch_to_repository(
        self,
        repository: BronzeRepository,
    ) -> list[BronzeAsset]:
        article_payloads = self._load_local_article_payloads(repository)
        legacy_text_payloads = self._load_local_legacy_text_payloads(repository)

        if not article_payloads and not legacy_text_payloads:
            raise RuntimeError(
                "Aucun artefact exploitable trouvé dans le bronze raw Légifrance. "
                "Dépose des articles JSON/XML dans raw/articles ou raw/legi_bulk/articles, "
                "et/ou des .txt dans raw/legacy_text_sources."
            )

        assets: list[BronzeAsset] = []
        for payload in article_payloads:
            payload_path = repository.save_article_payload(payload["short_id"], payload)
            assets.append(
                BronzeAsset(
                    asset_type="article",
                    short_id=payload["short_id"],
                    payload=payload,
                    payload_path=payload_path,
                )
            )

        for payload in legacy_text_payloads:
            payload_path = repository.save_legacy_text_payload(payload["short_id"], payload)
            assets.append(
                BronzeAsset(
                    asset_type="legacy_text",
                    short_id=payload["short_id"],
                    payload=payload,
                    payload_path=payload_path,
                )
            )

        repository.save_manifest(
            {
                "run_id": utc_now_iso().replace(":", "").replace(".", ""),
                "created_at": utc_now_iso(),
                "article_asset_count": sum(asset.asset_type == "article" for asset in assets),
                "legacy_text_asset_count": sum(asset.asset_type == "legacy_text" for asset in assets),
                "raw_articles_dir": str(repository.articles_dir),
                "raw_bulk_articles_dir": str(repository.bulk_articles_dir),
                "raw_legacy_text_sources_dir": str(repository.legacy_text_sources_dir),
                "assets": [str(asset.payload_path) for asset in assets],
            }
        )
        return assets
