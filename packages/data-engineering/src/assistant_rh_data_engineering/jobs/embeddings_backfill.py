from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import requests
from dotenv import dotenv_values, load_dotenv

from assistant_rh_data_engineering.utils.helpers import vector_to_pgvector

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill générique des embeddings DB à partir d'un manifest JSON."
    )
    parser.add_argument("--config", required=True, help="Manifest JSON de tables/colonnes d'embedding.")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--m3-model", default="BAAI/bge-m3")
    parser.add_argument("--m3-batch-size", type=int, default=64)
    parser.add_argument("--m3-device", default="cpu")
    parser.add_argument("--bge-model", default="bge-multilingual-gemma2")
    parser.add_argument("--bge-workers", type=int, default=2)
    parser.add_argument("--bge-batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-table")
    parser.add_argument("--only-column")
    return parser


def load_table_specs(config_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        specs = payload.get("tables") or []
    elif isinstance(payload, list):
        specs = payload
    else:
        specs = []
    normalized: list[dict[str, Any]] = []
    for spec in specs:
        table = str(spec.get("table") or "").strip()
        id_column = str(spec.get("id_column") or "").strip()
        text_column = str(spec.get("text_column") or "").strip()
        embeddings = spec.get("embeddings") or []
        if not table or not id_column or not text_column or not embeddings:
            continue
        normalized.append(
            {
                "table": table,
                "id_column": id_column,
                "text_column": text_column,
                "embeddings": embeddings,
            }
        )
    if not normalized:
        raise RuntimeError(f"Aucune table exploitable trouvée dans {config_path}.")
    return normalized


def _load_dotenv_key_candidates(env_path: Path, key_name: str) -> list[str]:
    if not env_path.exists():
        return []
    values: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != key_name:
            continue
        candidate = value.strip().strip('"').strip("'")
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if norm == 0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]


class ScalewayBgeClient:
    def __init__(self, env_path: Path, model_name: str):
        self.base_url = (
            os.getenv("SCALEWAY_BASE_URL")
            or dotenv_values(env_path).get("SCALEWAY_BASE_URL")
            or "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1"
        ).rstrip("/")
        self.model_name = model_name
        self.api_key = self._resolve_api_key(env_path)

    def _resolve_api_key(self, env_path: Path) -> str:
        candidates: list[str] = []
        env_value = os.getenv("SCALEWAY_API_KEY", "").strip()
        if env_value:
            candidates.append(env_value)
        for candidate in _load_dotenv_key_candidates(env_path, "SCALEWAY_API_KEY"):
            if candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            raise RuntimeError("Aucune clé SCALEWAY_API_KEY trouvée pour embedding_bge_scw.")
        for candidate in candidates:
            if self._is_valid_key(candidate):
                return candidate
        raise RuntimeError("Aucune SCALEWAY_API_KEY candidate ne permet d'appeler l'API embeddings Scaleway.")

    def _is_valid_key(self, api_key: str) -> bool:
        try:
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model_name, "input": "test"},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            return bool(payload.get("data"))
        except Exception:
            return False

    def embed_text(self, text: str) -> list[float]:
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                response = requests.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model_name, "input": text},
                    timeout=30,
                )
                if response.status_code == 429:
                    time.sleep(min(30, 2**attempt))
                    continue
                response.raise_for_status()
                return response.json()["data"][0]["embedding"]
            except Exception as exc:
                last_error = exc
                time.sleep(min(30, 2**attempt))
        if last_error is not None:
            raise last_error
        raise RuntimeError("Echec embedding_bge_scw sans erreur explicite.")


def fetch_missing_rows(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    id_column: str,
    text_column: str,
    embedding_column: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT {id_column} AS id, {text_column} AS text
        FROM {schema}.{table}
        WHERE {embedding_column} IS NULL
          AND COALESCE({text_column}, '') <> ''
        ORDER BY {id_column}
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(query)
        return [{"id": row[0], "text": row[1]} for row in cur.fetchall()]


def update_embeddings(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    id_column: str,
    embedding_column: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND column_name = 'updated_at'
            """,
            (schema, table),
        )
        has_updated_at = cur.fetchone() is not None

    set_clauses = [f"{embedding_column} = %(vector)s::vector"]
    if has_updated_at:
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    query = f"""
        UPDATE {schema}.{table}
        SET {", ".join(set_clauses)}
        WHERE {id_column} = %(id)s
    """
    payload = [
        {"id": row["id"], "vector": vector_to_pgvector(row["vector"])}
        for row in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(query, payload)
    conn.commit()
    return len(rows)


def backfill_m3(
    conn: psycopg.Connection,
    schema: str,
    table_spec: dict[str, Any],
    embedding_column: str,
    model_name: str,
    batch_size: int,
    device: str,
    limit: int | None,
) -> int:
    from sentence_transformers import SentenceTransformer

    rows = fetch_missing_rows(
        conn,
        schema,
        table_spec["table"],
        table_spec["id_column"],
        table_spec["text_column"],
        embedding_column,
        limit,
    )
    if not rows:
        return 0
    model = SentenceTransformer(model_name, device=device)
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        texts = [str(row["text"]) for row in batch]
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        prepared = [
            {"id": row["id"], "vector": vectors[index].astype("float32").tolist()}
            for index, row in enumerate(batch)
        ]
        total += update_embeddings(
            conn,
            schema,
            table_spec["table"],
            table_spec["id_column"],
            embedding_column,
            prepared,
        )
    return total


def backfill_bge_scaleway(
    conn: psycopg.Connection,
    schema: str,
    table_spec: dict[str, Any],
    embedding_column: str,
    env_path: Path,
    model_name: str,
    workers: int,
    batch_size: int,
    limit: int | None,
) -> int:
    rows = fetch_missing_rows(
        conn,
        schema,
        table_spec["table"],
        table_spec["id_column"],
        table_spec["text_column"],
        embedding_column,
        limit,
    )
    if not rows:
        return 0
    client = ScalewayBgeClient(env_path=env_path, model_name=model_name)
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            vectors = list(pool.map(client.embed_text, [str(row["text"]) for row in batch]))
        prepared = [
            {"id": row["id"], "vector": _normalize_vector(list(vectors[index]))}
            for index, row in enumerate(batch)
        ]
        total += update_embeddings(
            conn,
            schema,
            table_spec["table"],
            table_spec["id_column"],
            embedding_column,
            prepared,
        )
    return total


def main() -> int:
    args = build_parser().parse_args()
    env_path = Path(args.env_file)
    load_dotenv(env_path)
    dsn = os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"{args.dsn_env} manquant.")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    table_specs = load_table_specs(config_path)
    if args.only_table:
        table_specs = [spec for spec in table_specs if spec["table"] == args.only_table]
    if not table_specs:
        raise SystemExit("Aucune table sélectionnée pour le backfill embeddings.")

    summary: dict[str, Any] = {
        "config": str(config_path),
        "schema": args.schema,
        "tables": {},
    }
    with psycopg.connect(dsn) as conn:
        for table_spec in table_specs:
            table_summary: dict[str, int] = {}
            for embedding_spec in table_spec["embeddings"]:
                embedding_column = str(embedding_spec.get("column") or "").strip()
                algorithm = str(embedding_spec.get("algorithm") or "").strip().lower()
                if not embedding_column or not algorithm:
                    continue
                if args.only_column and embedding_column != args.only_column:
                    continue
                if algorithm in {"m3", "embedding_m3", "bge-m3"}:
                    table_summary[embedding_column] = backfill_m3(
                        conn,
                        args.schema,
                        table_spec,
                        embedding_column,
                        model_name=args.m3_model,
                        batch_size=args.m3_batch_size,
                        device=args.m3_device,
                        limit=args.limit,
                    )
                elif algorithm in {"bge_scaleway", "embedding_bge_scw", "bge_scw"}:
                    table_summary[embedding_column] = backfill_bge_scaleway(
                        conn,
                        args.schema,
                        table_spec,
                        embedding_column,
                        env_path=env_path,
                        model_name=args.bge_model,
                        workers=args.bge_workers,
                        batch_size=args.bge_batch_size,
                        limit=args.limit,
                    )
                else:
                    raise RuntimeError(
                        f"Algorithme d'embedding non supporté pour {table_spec['table']}.{embedding_column}: {algorithm}"
                    )
            summary["tables"][table_spec["table"]] = table_summary

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
