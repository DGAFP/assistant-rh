from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ObjectStorageConfig:
    region: str
    access_key: str
    secret_key: str
    bucket_bronze: str
    bucket_silver: str
    bucket_gold: str
    prefix_staging: str
    prefix_prod: str

    @property
    def endpoint_url(self) -> str:
        return f"https://s3.{self.region}.scw.cloud"

    def prefix_for_env(self, target_env: str) -> str:
        if target_env == "prod":
            return self.prefix_prod.strip("/")
        return self.prefix_staging.strip("/")

    @classmethod
    def from_env(cls) -> "ObjectStorageConfig":
        return cls(
            region=os.getenv("SCW_DEFAULT_REGION", "fr-par"),
            access_key=os.getenv("SCW_ACCESS_KEY", ""),
            secret_key=os.getenv("SCW_SECRET_KEY", ""),
            bucket_bronze=os.getenv("SCW_BUCKET_BRONZE", ""),
            bucket_silver=os.getenv("SCW_BUCKET_SILVER", ""),
            bucket_gold=os.getenv("SCW_BUCKET_GOLD", ""),
            prefix_staging=os.getenv("SCW_PREFIX_STAGING", "staging/service_public"),
            prefix_prod=os.getenv("SCW_PREFIX_PROD", "prod/service_public"),
        )


@dataclass(frozen=True)
class ObjectStorageObject:
    bucket: str
    key: str
    size: int | None = None
    last_modified: str | None = None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class ScalewayObjectStorageSync:
    def __init__(self, config: ObjectStorageConfig):
        self.config = config
        if not shutil.which("aws"):
            raise RuntimeError("aws CLI is required to sync pipeline artifacts to Scaleway Object Storage.")

    def _base_env(self) -> dict[str, str]:
        if not self.config.access_key or not self.config.secret_key:
            raise RuntimeError("SCW_ACCESS_KEY and SCW_SECRET_KEY are required for Object Storage sync.")
        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = self.config.access_key
        env["AWS_SECRET_ACCESS_KEY"] = self.config.secret_key
        env["AWS_DEFAULT_REGION"] = self.config.region
        return env

    def _sync_dir(
        self,
        source_dir: Path,
        bucket: str,
        prefix: str,
        *,
        delete: bool = False,
    ) -> None:
        if not source_dir.exists():
            return
        dest = f"s3://{bucket}/{prefix}/"
        cmd = [
            "aws",
            "--endpoint-url",
            self.config.endpoint_url,
            "s3",
            "sync",
            str(source_dir),
            dest,
        ]
        if delete:
            cmd.append("--delete")
        subprocess.run(cmd, check=True, env=self._base_env())

    def _run_capture(self, cmd: list[str]) -> str:
        result = subprocess.run(
            cmd,
            check=True,
            env=self._base_env(),
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _download_dir(self, bucket: str, prefix: str, destination_dir: Path) -> None:
        destination_dir.mkdir(parents=True, exist_ok=True)
        source = f"s3://{bucket}/{prefix}/"
        cmd = [
            "aws",
            "--endpoint-url",
            self.config.endpoint_url,
            "s3",
            "sync",
            source,
            str(destination_dir),
            "--no-progress",
        ]
        subprocess.run(cmd, check=True, env=self._base_env())

    def _bucket_for_layer(self, layer: str) -> str:
        if layer == "bronze":
            return self.config.bucket_bronze
        if layer == "silver":
            return self.config.bucket_silver
        if layer == "gold":
            return self.config.bucket_gold
        raise ValueError(f"Unsupported layer: {layer}")

    def medallion_prefix(
        self,
        target_env: str,
        layer: str,
        source_name: str = "service_public",
        suffix: str = "",
    ) -> tuple[str, str]:
        env_prefix = self.config.prefix_for_env(target_env)
        prefix = f"{env_prefix}/{layer}/{source_name}".strip("/")
        if suffix:
            prefix = f"{prefix}/{suffix.lstrip('/')}".strip("/")
        return self._bucket_for_layer(layer), prefix

    def list_objects(self, bucket: str, prefix: str) -> list[ObjectStorageObject]:
        objects: list[ObjectStorageObject] = []
        continuation_token: str | None = None

        while True:
            cmd = [
                "aws",
                "--endpoint-url",
                self.config.endpoint_url,
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--prefix",
                prefix,
                "--output",
                "json",
            ]
            if continuation_token:
                cmd.extend(["--continuation-token", continuation_token])

            payload = json.loads(self._run_capture(cmd) or "{}")
            for item in payload.get("Contents") or []:
                key = str(item.get("Key") or "").strip()
                if not key:
                    continue
                objects.append(
                    ObjectStorageObject(
                        bucket=bucket,
                        key=key,
                        size=int(item["Size"]) if item.get("Size") is not None else None,
                        last_modified=str(item.get("LastModified") or "").strip() or None,
                    )
                )

            if not payload.get("IsTruncated"):
                break
            continuation_token = str(payload.get("NextContinuationToken") or "").strip() or None
            if not continuation_token:
                break

        return objects

    def list_medallion_objects(
        self,
        target_env: str,
        layer: str,
        source_name: str = "service_public",
        suffix: str = "",
    ) -> list[ObjectStorageObject]:
        bucket, prefix = self.medallion_prefix(target_env, layer, source_name, suffix)
        return self.list_objects(bucket, prefix)

    def read_text_object(self, obj: ObjectStorageObject) -> str:
        return self._run_capture(
            [
                "aws",
                "--endpoint-url",
                self.config.endpoint_url,
                "s3",
                "cp",
                obj.uri,
                "-",
            ]
        )

    def upload_object(self, source: Path, bucket: str, key: str) -> ObjectStorageObject:
        subprocess.run(
            [
                "aws",
                "--endpoint-url",
                self.config.endpoint_url,
                "s3",
                "cp",
                str(source),
                f"s3://{bucket}/{key}",
            ],
            check=True,
            env=self._base_env(),
        )
        return ObjectStorageObject(bucket=bucket, key=key)

    def download_object(self, obj: ObjectStorageObject, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "aws",
                "--endpoint-url",
                self.config.endpoint_url,
                "s3",
                "cp",
                obj.uri,
                str(destination),
            ],
            check=True,
            env=self._base_env(),
        )
        return destination

    def download_objects(
        self,
        objects: list[ObjectStorageObject],
        destination_dir: Path,
        *,
        chunk_size: int = 200,
    ) -> list[Path]:
        if not objects:
            return []

        destination_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        groups: dict[tuple[str, str], list[ObjectStorageObject]] = {}
        for obj in objects:
            parent_key = str(Path(obj.key).parent).strip(".")
            groups.setdefault((obj.bucket, parent_key), []).append(obj)

        for (bucket, prefix), group_objects in groups.items():
            if not prefix:
                continue

            filenames = sorted({Path(obj.key).name for obj in group_objects if Path(obj.key).name})
            group_destination = destination_dir / Path(prefix).name
            group_destination.mkdir(parents=True, exist_ok=True)

            source = f"s3://{bucket}/{prefix}/"
            for index in range(0, len(filenames), chunk_size):
                batch = filenames[index : index + chunk_size]
                cmd = [
                    "aws",
                    "--endpoint-url",
                    self.config.endpoint_url,
                    "s3",
                    "cp",
                    source,
                    str(group_destination),
                    "--recursive",
                    "--exclude",
                    "*",
                ]
                for filename in batch:
                    cmd.extend(["--include", filename])
                subprocess.run(
                    cmd,
                    check=True,
                    env=self._base_env(),
                    capture_output=True,
                    text=True,
                )

            for filename in filenames:
                path = group_destination / filename
                if path.exists():
                    downloaded.append(path)

        return downloaded

    def sync_medallion_root(
        self,
        lake_root: Path,
        target_env: str,
        source_name: str = "service_public",
        *,
        delete: bool = False,
        include_layers: tuple[str, ...] = ("bronze", "silver", "gold"),
    ) -> dict[str, str]:
        env_prefix = self.config.prefix_for_env(target_env)
        bronze_prefix = f"{env_prefix}/bronze/{source_name}".strip("/")
        silver_prefix = f"{env_prefix}/silver/{source_name}".strip("/")
        gold_prefix = f"{env_prefix}/gold/{source_name}".strip("/")

        # ``include_layers`` restreint la synchro : un médaillon qui LIT le bronze
        # depuis l'Object Storage (``--from-object-storage``) ne le POSSÈDE pas —
        # le synchroniser (surtout avec ``delete``) écraserait/supprimerait le
        # bronze distant (produit par le bulk dump) à partir d'un bronze local vide.
        if "bronze" in include_layers:
            self._sync_dir(
                lake_root / "bronze",
                self.config.bucket_bronze,
                bronze_prefix,
                delete=delete,
            )
        if "silver" in include_layers:
            self._sync_dir(
                lake_root / "silver",
                self.config.bucket_silver,
                silver_prefix,
                delete=delete,
            )
        if "gold" in include_layers:
            self._sync_dir(
                lake_root / "gold",
                self.config.bucket_gold,
                gold_prefix,
                delete=delete,
            )
        # Ne reporter QUE les couches réellement synchronisées (P3 revue #317) :
        # annoncer une destination bronze non synchronisée serait trompeur.
        destinations = {
            "bronze": f"s3://{self.config.bucket_bronze}/{bronze_prefix}/",
            "silver": f"s3://{self.config.bucket_silver}/{silver_prefix}/",
            "gold": f"s3://{self.config.bucket_gold}/{gold_prefix}/",
        }
        return {layer: uri for layer, uri in destinations.items() if layer in include_layers}

    def download_medallion_root(
        self,
        lake_root: Path,
        target_env: str,
        source_name: str = "service_public",
        include_layers: tuple[str, ...] = ("bronze", "silver", "gold"),
    ) -> dict[str, str]:
        env_prefix = self.config.prefix_for_env(target_env)
        destinations: dict[str, str] = {}

        if "bronze" in include_layers:
            bronze_prefix = f"{env_prefix}/bronze/{source_name}".strip("/")
            self._download_dir(
                self.config.bucket_bronze,
                bronze_prefix,
                lake_root / "bronze",
            )
            destinations["bronze"] = f"s3://{self.config.bucket_bronze}/{bronze_prefix}/"

        if "silver" in include_layers:
            silver_prefix = f"{env_prefix}/silver/{source_name}".strip("/")
            self._download_dir(
                self.config.bucket_silver,
                silver_prefix,
                lake_root / "silver",
            )
            destinations["silver"] = f"s3://{self.config.bucket_silver}/{silver_prefix}/"

        if "gold" in include_layers:
            gold_prefix = f"{env_prefix}/gold/{source_name}".strip("/")
            self._download_dir(
                self.config.bucket_gold,
                gold_prefix,
                lake_root / "gold",
            )
            destinations["gold"] = f"s3://{self.config.bucket_gold}/{gold_prefix}/"

        return destinations
