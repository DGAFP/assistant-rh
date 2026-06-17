from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import requests
from assistant_rh_shared import Config
from dotenv import load_dotenv
from psycopg import sql

from assistant_rh_data_engineering.utils.helpers import vector_to_pgvector

logger = logging.getLogger(__name__)

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill générique des embeddings DB à partir d'un manifest JSON.")
    parser.add_argument("--config", required=True, help="Manifest JSON de tables/colonnes d'embedding.")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--m3-model", default="BAAI/bge-m3")
    parser.add_argument("--m3-batch-size", type=int, default=64)
    parser.add_argument("--m3-device", default="cpu")
    parser.add_argument("--bge-model", default="bge-multilingual-gemma2")
    parser.add_argument("--bge-base-url", help="Base URL Scaleway pour embedding_bge_scw.")
    parser.add_argument("--bge-workers", type=int, default=2)
    parser.add_argument("--bge-batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-table")
    parser.add_argument("--only-column")
    parser.add_argument(
        "--check-only",
        "--dry-run",
        dest="check_only",
        action="store_true",
        help=(
            "Mode audit read-only : imprime la couverture d'embedding par "
            "table/colonne sans charger de modèle, sans appeler d'API, et "
            "sans écrire en base. Code retour 0 si couverture complète, "
            "1 sinon."
        ),
    )
    parser.add_argument(
        "--coverage-min-pct",
        type=float,
        default=None,
        help=("Seuil de couverture (0-100) par colonne. Avec --check-only, le code retour est 1 dès qu'une colonne est sous le seuil."),
    )
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


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if norm == 0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]


def _filter_table_specs(
    table_specs: list[dict[str, Any]],
    *,
    only_table: str | None,
    only_column: str | None,
) -> list[dict[str, Any]]:
    """Restreint la sélection de tables/colonnes selon --only-table / --only-column.

    Les colonnes sans correspondance après filtrage sont conservées au niveau
    table : un manifest qui ne contient que les colonnes demandées peut
    produire un filtre agressif, mais on conserve la table si elle est
    explicitement demandée par --only-table.
    """
    filtered: list[dict[str, Any]] = []
    for spec in table_specs:
        if only_table and spec["table"] != only_table:
            continue
        embeddings = spec.get("embeddings") or []
        if only_column:
            embeddings = [spec_embedding for spec_embedding in embeddings if str(spec_embedding.get("column") or "").strip() == only_column]
        if not embeddings:
            continue
        filtered.append({**spec, "embeddings": embeddings})
    return filtered


def _table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        return cur.fetchone() is not None


def audit_embedding_coverage(
    conn: psycopg.Connection,
    schema: str,
    table_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calcule la couverture d'embedding par table/colonne en SQL.

    N'importe aucun modèle d'embedding, n'appelle aucune API externe et
    n'écrit rien en base. Une seule requête ``SELECT`` agrégée est émise par
    couple ``(table, embedding_column)`` afin de rester compatible avec des
    tables volumineuses.
    """
    report: dict[str, Any] = {
        "schema": schema,
        "tables": {},
        "missing_tables": [],
    }
    for spec in table_specs:
        table = spec["table"]
        if not _table_exists(conn, schema, table):
            report["missing_tables"].append(table)
            continue
        table_summary: dict[str, Any] = {}
        for embedding_spec in spec["embeddings"]:
            column = str(embedding_spec.get("column") or "").strip()
            if not column:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        """
                        SELECT
                            COUNT(*) AS total,
                            COUNT({col}) FILTER (WHERE {col} IS NOT NULL)
                                AS non_null,
                            COUNT(*) FILTER (
                                WHERE {col} IS NULL
                                  AND COALESCE({text_col}, '') <> ''
                            ) AS missing_with_text,
                            COUNT(*) FILTER (
                                WHERE COALESCE({text_col}, '') = ''
                            ) AS empty_text
                        FROM {schema}.{table}
                        """
                    ).format(
                        col=sql.Identifier(column),
                        text_col=sql.Identifier(spec["text_column"]),
                        schema=sql.Identifier(schema),
                        table=sql.Identifier(table),
                    )
                )
                row = cur.fetchone()
            total, non_null, missing_with_text, empty_text = (int(value or 0) for value in row)
            coverage_pct = round(100.0 * non_null / total, 2) if total else 100.0
            table_summary[column] = {
                "total": total,
                "non_null": non_null,
                "missing_with_text": missing_with_text,
                "empty_text": empty_text,
                "coverage_pct": coverage_pct,
            }
        report["tables"][table] = table_summary
    return report


def evaluate_coverage_report(
    report: dict[str, Any],
    *,
    coverage_min_pct: float | None,
) -> tuple[int, list[str]]:
    """Évalue un rapport d'audit et renvoie ``(exit_code, problems)``.

    - ``exit_code`` vaut ``1`` si au moins une colonne sous le seuil ou si
      une table attendue est absente, sinon ``0``.
    - ``problems`` liste les constats lisibles, destinés à être imprimés en
      plus du JSON.
    """
    problems: list[str] = []
    threshold = coverage_min_pct if coverage_min_pct is not None else 100.0
    for table in report.get("missing_tables", []) or []:
        problems.append(f"Table absente: {table}")
    for table, columns in (report.get("tables") or {}).items():
        for column, stats in (columns or {}).items():
            coverage_pct = float(stats.get("coverage_pct") or 0.0)
            if coverage_pct < threshold:
                problems.append(
                    f"{table}.{column}: couverture {coverage_pct}% < seuil {threshold}% "
                    f"(total={stats.get('total')}, non_null={stats.get('non_null')}, "
                    f"manquant_avec_texte={stats.get('missing_with_text')})"
                )
    return (1 if problems else 0), problems


class ScalewayBgeClient:
    def __init__(self, model_name: str, base_url: str | None = None, session: requests.Session | None = None):
        config = Config()
        resolved_base_url = (base_url or "").strip() or config.scaleway_base_url
        if not resolved_base_url:
            raise RuntimeError("SCALEWAY_BASE_URL manquant pour embedding_bge_scw (ou passer --bge-base-url).")
        self.base_url = resolved_base_url.rstrip("/")
        self.model_name = model_name
        if not config.scaleway_api_key:
            raise RuntimeError("Aucune clé SCALEWAY_API_KEY trouvée pour embedding_bge_scw.")
        self.api_key = config.scaleway_api_key
        self.session = session

    def _post_embeddings(self, text: str) -> requests.Response:
        post = self.session.post if self.session is not None else requests.post
        return post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model_name, "input": text},
            timeout=30,
        )

    def embed_text(self, text: str) -> list[float]:
        max_attempts = 6
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = self._post_embeddings(text)
                response.raise_for_status()
                payload = response.json()
                embedding = payload["data"][0]["embedding"]
                if not isinstance(embedding, list):
                    raise ValueError("Réponse embeddings Scaleway invalide: embedding absent ou non-list.")
                return embedding
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                # Seuls 429 et 5xx sont transitoires; les autres 4xx (clé invalide,
                # modèle inconnu...) ne se résoudront pas en réessayant.
                if status_code != 429 and not (status_code is not None and status_code >= 500):
                    raise
                last_error = exc
                logger.debug("Erreur HTTP %s embedding_bge_scw, retry %s/%s.", status_code, attempt + 1, max_attempts)
            except requests.RequestException as exc:
                last_error = exc
                logger.debug("Erreur réseau embedding_bge_scw, retry %s/%s: %s", attempt + 1, max_attempts, exc)
            if attempt < max_attempts - 1:
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
    payload = [{"id": row["id"], "vector": vector_to_pgvector(row["vector"])} for row in rows]
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
        prepared = [{"id": row["id"], "vector": vectors[index].astype("float32").tolist()} for index, row in enumerate(batch)]
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
    model_name: str,
    base_url: str | None,
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
    total = 0
    client = ScalewayBgeClient(model_name=model_name, base_url=base_url)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            vectors = list(pool.map(client.embed_text, [str(row["text"]) for row in batch]))
            prepared = [{"id": row["id"], "vector": _normalize_vector(list(vectors[index]))} for index, row in enumerate(batch)]
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
    load_dotenv(Path(args.env_file))
    # Clé d'environnement dynamique (--dsn-env): reste hors de Config.
    dsn = os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"{args.dsn_env} manquant.")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    table_specs = load_table_specs(config_path)
    table_specs = _filter_table_specs(
        table_specs,
        only_table=args.only_table,
        only_column=args.only_column,
    )
    if not table_specs:
        raise SystemExit("Aucune table sélectionnée pour le backfill embeddings.")

    summary: dict[str, Any] = {
        "config": str(config_path),
        "schema": args.schema,
        "only_table": args.only_table,
        "only_column": args.only_column,
        "check_only": bool(args.check_only),
        "tables": {},
    }

    with psycopg.connect(dsn) as conn:
        if args.check_only:
            # Mode audit read-only: pas d'import de modèle d'embedding, pas
            # d'appel API, pas d'écriture en base.
            report = audit_embedding_coverage(conn, args.schema, table_specs)
            exit_code, problems = evaluate_coverage_report(
                report,
                coverage_min_pct=args.coverage_min_pct,
            )
            summary.update(report)
            summary["problems"] = problems
            summary["exit_code"] = exit_code
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if problems:
                for problem in problems:
                    logger.warning(problem)
            return exit_code

        for table_spec in table_specs:
            table_summary: dict[str, int] = {}
            for embedding_spec in table_spec["embeddings"]:
                embedding_column = str(embedding_spec.get("column") or "").strip()
                algorithm = str(embedding_spec.get("algorithm") or "").strip().lower()
                if not embedding_column or not algorithm:
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
                        model_name=args.bge_model,
                        base_url=args.bge_base_url,
                        workers=args.bge_workers,
                        batch_size=args.bge_batch_size,
                        limit=args.limit,
                    )
                else:
                    raise RuntimeError(f"Algorithme d'embedding non supporté pour {table_spec['table']}.{embedding_column}: {algorithm}")
            summary["tables"][table_spec["table"]] = table_summary

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
