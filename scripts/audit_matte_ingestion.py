#!/usr/bin/env python3
"""
Audit read-only / offline de l'ingestion source MATTE (issue #103).

Ce script est volontairement **read-only** et **offline** par défaut :

- il inspecte le repo (présence de notebooks, parsing statique de la liste
  de PDF d'entrée, présence d'artefacts générés sous ``data/out``) ;
- il émet des requêtes SQL de couverture en lecture seule ;
- il peut, **uniquement** si la variable d'environnement ``MATTE_AUDIT_DSN``
  est définie, exécuter ces requêtes en lecture seule contre la base.

**Aucune** écriture n'est jamais émise : aucun ``UPDATE``, ``INSERT``,
``DELETE``, ``CREATE INDEX``, ``ALTER``, ``DROP``. Le script refuse
explicitement de connecter à la base si ``MATTE_AUDIT_DSN`` est absente.

Sortie : un rapport JSON stable, lisible, imprimable en stdout.

Usage :

    # Mode par défaut (audit repo + artefacts + émission SQL)
    uv run python scripts/audit_matte_ingestion.py --repo-root .

    # Mode SQL only (rapport + requêtes, pas d'exécution)
    uv run python scripts/audit_matte_ingestion.py --repo-root . --sql-only

    # Mode DB read-only (exige MATTE_AUDIT_DSN, refuse en CI)
    MATTE_AUDIT_DSN=postgresql://... uv run python scripts/audit_matte_ingestion.py \\
        --repo-root . --db-readonly

Voir ``docs/MATTE_SOURCE_INGESTION_AUDIT.md`` pour le contexte de l'audit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set  # noqa: F401

# ---------------------------------------------------------------------------
# Constantes —alignées sur le runtime canonique
# ---------------------------------------------------------------------------

#: Colonne d'embedding canonique de retrieval (Albert / BAAI/bge-m3, 1024-dim).
#: Source : packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py
#: (EMBEDDING_COLUMN_MAP) et config.py (CHUNK_TABLES["matte"].embed_col_albert).
CANONICAL_EMBED_COL_ALBERT = "embedding_m3"

#: Colonne d'embedding BGE-Scaleway (fallback embeddings, 3584-dim).
CANONICAL_EMBED_COL_BGE = "embedding_bge_scw"

#: Colonnes d'embedding qui coexistent sur rag_chunks_matte (audit 06 §1.4).
#: Aucune n'est renommée ou supprimée par cet audit ; la liste est
#: utilisée pour la requête SQL d'introspection couverture.
KNOWN_EMBED_COLS: List[str] = [
    "embedding_m3",
    "embedding_bge_scw",
    "embedding_qwen3",
    "embedding_ctx",
    "embedding_bge",
]

#: Table MATTE canonique de retrieval (RAG V3 pipeline).
CANONICAL_TABLE = "rag_chunks_matte"

#: Nom historique référencé par certains notebooks.
LEGACY_TABLE_HISTORICAL = "rag_chunks_3"

#: Notebooks attendus d'après scripts/README.md et .env.example.
EXPECTED_NOTEBOOKS: List[str] = [
    "scripts/extract_matte.ipynb",
    "scripts/amelioration_matte.ipynb",
    "scripts/ingestion_matte.ipynb",
]

#: Variables MATTE_ dans .env.example à vérifier.
EXPECTED_ENV_VARS: List[str] = [
    "MATTE_BASE_IN",
    "MATTE_BASE_OUT",
    "MATTE_AMELIORATION_CLEAN_JSONL",
    "MATTE_AMELIORATION_IN_JSONL",
    "MATTE_AMELIORATION_OUT_PARQUET",
    "MATTE_AMELIORATION_OUT_NPY",
    "MATTE_AMELIORATION_OUT_JSONL_WITH_EMB",
    "MATTE_IN_JSONL_WITH_EMB",
    "MATTE_TABLE",
]

#: Variables d'environnement qui déclenchent le mode DB.
DB_DSN_ENV = "MATTE_AUDIT_DSN"


# ---------------------------------------------------------------------------
# Dataclass rapport
# ---------------------------------------------------------------------------


@dataclass
class FileFinding:
    """Un constat sur un fichier du repo."""

    path: str
    present: bool
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactFinding:
    """Un constat sur un artefact généré."""

    path: str
    present: bool
    row_count: Optional[int] = None
    unique_hash_id: Optional[int] = None
    empty_text: Optional[int] = None
    embedding_dim: Optional[int] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SqlStatement:
    """Une requête SQL émise (jamais exécutée en mode --sql-only)."""

    name: str
    description: str
    sql: str
    requires_db: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AuditReport:
    """Rapport d'audit sérialisable en JSON."""

    repo_root: str
    canonical_table: str
    canonical_embed_col_albert: str
    canonical_embed_col_bge: str
    notebooks: List[FileFinding] = field(default_factory=list)
    pdf_paths_declared: List[str] = field(default_factory=list)
    env_vars: List[FileFinding] = field(default_factory=list)
    artifacts: List[ArtifactFinding] = field(default_factory=list)
    sql_statements: List[SqlStatement] = field(default_factory=list)
    db_results: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "canonical_table": self.canonical_table,
            "canonical_embed_col_albert": self.canonical_embed_col_albert,
            "canonical_embed_col_bge": self.canonical_embed_col_bge,
            "notebooks": [n.to_dict() for n in self.notebooks],
            "pdf_paths_declared": self.pdf_paths_declared,
            "env_vars": [v.to_dict() for v in self.env_vars],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "sql_statements": [stmt.to_dict() for stmt in self.sql_statements],
            "db_results": self.db_results,
            "diagnostics": list(self.diagnostics),
            "notes": list(self.notes),
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# SQL statements —tous en lecture seule
# ---------------------------------------------------------------------------

SQL_COVERAGE_EMBEDDINGS = f"""
-- Couverture embeddings sur {CANONICAL_TABLE}.
-- Lecture seule : compte les NULL par colonne d'embedding.
SELECT
  COUNT(*)                                                    AS total_rows,
  COUNT(*) FILTER (WHERE embedding_m3       IS NULL)          AS embedding_m3_null,
  COUNT(*) FILTER (WHERE embedding_bge_scw  IS NULL)          AS embedding_bge_scw_null,
  COUNT(*) FILTER (WHERE embedding_qwen3    IS NULL)          AS embedding_qwen3_null,
  COUNT(*) FILTER (WHERE embedding_ctx      IS NULL)          AS embedding_ctx_null,
  COUNT(*) FILTER (WHERE embedding_bge      IS NULL)          AS embedding_bge_null
FROM {CANONICAL_TABLE};
""".strip()


SQL_CANONICAL_COLUMNS = f"""
-- Liste des colonnes d'embedding réellement présentes (information_schema).
-- Lecture seule : introspection, pas d'écriture.
SELECT column_name, data_type, udt_name, character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = '{CANONICAL_TABLE}'
  AND column_name IN ('embedding_m3', 'embedding_bge_scw',
                      'embedding_qwen3', 'embedding_ctx', 'embedding_bge')
ORDER BY column_name;
""".strip()


SQL_DUPLICATE_HASH_IDS = f"""
-- Doublons de hash_id (PK) sur {CANONICAL_TABLE} : devrait toujours être 0.
SELECT hash_id, COUNT(*) AS n
FROM {CANONICAL_TABLE}
GROUP BY hash_id
HAVING COUNT(*) > 1
ORDER BY n DESC
LIMIT 20;
""".strip()


SQL_DUPLICATE_TEXT = f"""
-- Top-20 des (text) dupliqués : permet de quantifier la duplication
-- introduite par le role="TABLE" de amelioration_matte.ipynb.
SELECT
  text,
  COUNT(*)            AS n,
  COUNT(DISTINCT hash_id) AS distinct_hash_id
FROM {CANONICAL_TABLE}
GROUP BY text
HAVING COUNT(*) > 1
ORDER BY n DESC
LIMIT 20;
""".strip()


SQL_SECTION_FK_COVERAGE = f"""
-- Couverture des FK section_id / source_document_id sur {CANONICAL_TABLE}.
SELECT
  COUNT(*)                                                AS total,
  COUNT(*) FILTER (WHERE section_id        IS NULL)       AS section_id_null,
  COUNT(*) FILTER (WHERE source_document_id IS NULL)      AS source_document_id_null,
  COUNT(*) FILTER (WHERE short_id          IS NULL)       AS short_id_null
FROM {CANONICAL_TABLE};
""".strip()


SQL_EMPTY_TEXT = f"""
-- Lignes avec chunk_text / text vide : devraient toujours être 0.
SELECT
  COUNT(*) FILTER (WHERE chunk_text IS NULL OR chunk_text = '')   AS empty_chunk_text,
  COUNT(*) FILTER (WHERE text       IS NULL OR text       = '')   AS empty_text
FROM {CANONICAL_TABLE};
""".strip()


SQL_INDEXES = f"""
-- Index présents sur {CANONICAL_TABLE} (lecture seule).
-- Permet de confirmer l'absence d'index vectoriel sur embedding_m3.
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = '{CANONICAL_TABLE}'
ORDER BY indexname;
""".strip()


SQL_VECTORS_INFORMATION_SCHEMA = f"""
-- Colonnes vector(*) et leur dimension pour {CANONICAL_TABLE}.
-- Lecture seule.
SELECT
  a.attname                                 AS column_name,
  format_type(a.atttypid, a.atttypmod)      AS full_type,
  a.atttypmod - 4                           AS dim
FROM pg_attribute a
JOIN pg_class    c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type     t ON t.oid = a.atttypid
WHERE n.nspname = 'public'
  AND c.relname = '{CANONICAL_TABLE}'
  AND t.typname = 'vector'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attname;
""".strip()


SQL_STATEMENTS: List[SqlStatement] = [
    SqlStatement(
        name="coverage_embeddings",
        description=(f"Couverture embeddings par colonne sur {CANONICAL_TABLE}. Lecture seule."),
        sql=SQL_COVERAGE_EMBEDDINGS,
    ),
    SqlStatement(
        name="canonical_columns",
        description=("Colonnes d'embedding réellement présentes dans information_schema."),
        sql=SQL_CANONICAL_COLUMNS,
    ),
    SqlStatement(
        name="duplicate_hash_ids",
        description="Top-20 des hash_id (PK) dupliqués. Doit toujours être vide.",
        sql=SQL_DUPLICATE_HASH_IDS,
    ),
    SqlStatement(
        name="duplicate_text",
        description=("Top-20 des textes dupliqués (peut être non-vide à cause de role='TABLE')."),
        sql=SQL_DUPLICATE_TEXT,
    ),
    SqlStatement(
        name="section_fk_coverage",
        description=("Couverture des colonnes FK (section_id, source_document_id, short_id)."),
        sql=SQL_SECTION_FK_COVERAGE,
    ),
    SqlStatement(
        name="empty_text",
        description="Lignes avec chunk_text / text NULL ou vide.",
        sql=SQL_EMPTY_TEXT,
    ),
    SqlStatement(
        name="indexes",
        description=(f"Index présents sur {CANONICAL_TABLE} (incluant vectoriels)."),
        sql=SQL_INDEXES,
    ),
    SqlStatement(
        name="vector_columns_dim",
        description="Dimensions des colonnes pgvector sur la table.",
        sql=SQL_VECTORS_INFORMATION_SCHEMA,
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Regex pour parser la liste Python PDF_PATHS dans amelioration_matte.ipynb.
#: On accepte Path("…") ou Path('./…') sur une ou plusieurs lignes.
_PDF_PATH_PATTERN = re.compile(
    r"""Path\(\s*['"]([^'"]+\.pdf)['"]\s*\)""",
    re.IGNORECASE,
)


def parse_pdf_paths_from_notebook(notebook_path: Path) -> List[str]:
    """Parse la liste ``PDF_PATHS: List[Path] = [Path(...)]`` du notebook.

    Lecture **statique** du JSON .ipynb (pas d'exécution de code). On extrait
    les chaînes littérales de tous les appels ``Path("…pdf")``.

    Returns:
        Liste dédupliquée de chemins PDF (ordre d'apparition).
    """
    if not notebook_path.exists():
        return []
    try:
        data = json.loads(notebook_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Ne pas faire échouer l'audit : signaler dans errors.
        raise ValueError(f"Impossible de parser {notebook_path}: {exc}") from exc
    seen: Set[str] = set()
    ordered: List[str] = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        for line in cell.get("source", []):
            for match in _PDF_PATH_PATTERN.finditer(line):
                p = match.group(1).strip()
                if p and p not in seen:
                    seen.add(p)
                    ordered.append(p)
    return ordered


def parse_env_example(env_example_path: Path) -> Set[str]:
    """Lit .env.example et retourne l'ensemble des noms de variables
    déclarées (lignes ``KEY=…`` non commentées)."""
    if not env_example_path.exists():
        return set()
    keys: Set[str] = set()
    for raw in env_example_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def inspect_artifact(jsonl_path: Path) -> ArtifactFinding:
    """Inspecte un JSONL d'artefacts générés, sans jamais l'écrire.

    Hypothèse : un artefact ``*_with_emb.jsonl`` MATTE typique a les colonnes
    ``hash_id`` et ``text`` (ou ``chunk_text``) et ``embedding_m3``.
    """
    finding = ArtifactFinding(path=str(jsonl_path), present=False)
    if not jsonl_path.exists():
        finding.note = "Artefact absent — l'audit local est non bloquant."
        return finding
    finding.present = True
    row_count = 0
    hash_ids: Set[str] = set()
    empty_text = 0
    embedding_dim: Optional[int] = None
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                row_count += 1
                hid = row.get("hash_id")
                if isinstance(hid, str) and hid:
                    hash_ids.add(hid)
                text = row.get("text") or row.get("chunk_text") or ""
                if not text.strip():
                    empty_text += 1
                emb = row.get(CANONICAL_EMBED_COL_ALBERT)
                if isinstance(emb, list) and embedding_dim is None:
                    embedding_dim = len(emb)
    except OSError as exc:
        finding.note = f"Lecture impossible: {exc}"
        return finding
    finding.row_count = row_count
    finding.unique_hash_id = len(hash_ids)
    finding.empty_text = empty_text
    finding.embedding_dim = embedding_dim
    if row_count and len(hash_ids) != row_count:
        finding.note = f"⚠ {row_count - len(hash_ids)} doublons de hash_id sur {row_count} lignes."
    elif row_count == 0:
        finding.note = "⚠ Fichier vide."
    elif empty_text:
        finding.note = f"⚠ {empty_text} lignes avec texte vide."
    return finding


def run_db_readonly(
    dsn: str,
    statements: Sequence[SqlStatement],
) -> Dict[str, Any]:
    """Exécute les requêtes en lecture seule contre la base.

    Refuse tout statement contenant un mot-clé d'écriture. **Aucun** COMMIT
    n'est nécessaire : psycopg ouvre une transaction par défaut, on lit et on
    rollback systématiquement pour ne laisser aucune session trainer.
    """
    forbidden = re.compile(
        r"\b(insert|update|delete|create|drop|alter|truncate|grant|revoke|"
        r"copy|vacuum|cluster|reindex|refresh|lock|call|do|set|reset)\b",
        re.IGNORECASE,
    )
    results: Dict[str, Any] = {}
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - psycopg est runtime only
        return {"error": f"psycopg non disponible: {exc}"}

    for stmt in statements:
        if not stmt.requires_db:
            continue
        if forbidden.search(stmt.sql):
            results[stmt.name] = {"error": "refused: forbidden write keyword"}
            continue
        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(stmt.sql)
                    if cur.description is None:
                        results[stmt.name] = {"rows": []}
                        continue
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchall()
                    results[stmt.name] = {
                        "columns": cols,
                        "rows": [list(r) for r in rows],
                    }
                # Toujours rollback pour ne rien laisser trainer.
                conn.rollback()
        except Exception as exc:  # noqa: BLE001 — on remonte l'erreur
            results[stmt.name] = {"error": f"{type(exc).__name__}: {exc}"}
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_report(repo_root: Path, *, db_readonly: bool) -> AuditReport:
    report = AuditReport(
        repo_root=str(repo_root.resolve()),
        canonical_table=CANONICAL_TABLE,
        canonical_embed_col_albert=CANONICAL_EMBED_COL_ALBERT,
        canonical_embed_col_bge=CANONICAL_EMBED_COL_BGE,
    )

    # 1. Notebooks attendus
    for rel in EXPECTED_NOTEBOOKS:
        full = repo_root / rel
        present = full.exists()
        note = ""
        if rel == "scripts/amelioration_matte.ipynb" and not present:
            note = "Notebook de nettoyage absent — la chaîne d'ingestion est rompue."
        elif rel in ("scripts/extract_matte.ipynb", "scripts/ingestion_matte.ipynb") and not present:
            note = "Notebook référencé par scripts/README.md et .env.example mais absent du worktree — constaté sur origin/main."
        report.notebooks.append(FileFinding(path=rel, present=present, note=note))

    missing = [n.path for n in report.notebooks if not n.present]
    if missing:
        report.diagnostics.append("STALE_NOTEBOOKS: " + ", ".join(sorted(missing)))

    # 2. PDF déclarés dans amelioration_matte.ipynb
    amelio_nb = repo_root / "scripts/amelioration_matte.ipynb"
    try:
        report.pdf_paths_declared = parse_pdf_paths_from_notebook(amelio_nb)
    except ValueError as exc:
        report.errors.append(str(exc))
    if report.pdf_paths_declared:
        report.notes.append(f"{len(report.pdf_paths_declared)} PDF déclarés dans amelioration_matte.ipynb (parsing statique, non-exécution).")
    elif amelio_nb.exists():
        report.notes.append("Aucun PDF détecté dans amelioration_matte.ipynb (vérifier la regex / le format Path()).")

    # 3. Variables MATTE_ dans .env.example
    env_example = repo_root / ".env.example"
    declared = parse_env_example(env_example) if env_example.exists() else set()
    for var in EXPECTED_ENV_VARS:
        report.env_vars.append(
            FileFinding(
                path=var,
                present=var in declared,
                note=("" if var in declared else "Variable MATTE_ manquante dans .env.example"),
            )
        )
    missing_env = [v.path for v in report.env_vars if not v.present]
    if missing_env:
        report.diagnostics.append("STALE_ENV_VARS: " + ", ".join(sorted(missing_env)))

    # 4. Artefacts générés sous data/out (lecture seule, non bloquant)
    artifact_paths = [
        "data/out/chunked/matte_temps_travail_3pdf_clean.jsonl",
        "data/out/matte_temps_du_travail_amelioration_chunks_baai_bge_m3_with_emb.jsonl",
    ]
    for rel in artifact_paths:
        report.artifacts.append(inspect_artifact(repo_root / rel))

    # 5. SQL statements
    report.sql_statements = list(SQL_STATEMENTS)

    # 6. DB read-only ?
    if db_readonly:
        dsn = os.environ.get(DB_DSN_ENV, "").strip()
        if not dsn:
            report.errors.append(f"Mode --db-readonly demandé mais {DB_DSN_ENV} est vide ou non exporté. Abandon du mode DB.")
        else:
            report.notes.append(f"Mode DB read-only activé via {DB_DSN_ENV} — toute écriture est rejetée par construction.")
            report.db_results = run_db_readonly(dsn, SQL_STATEMENTS)
    else:
        report.notes.append(f"Mode --sql-only : aucune exécution SQL. Pour exécuter en read-only, exporter {DB_DSN_ENV} et passer --db-readonly.")

    return report


def render_markdown(report: AuditReport) -> str:
    """Sérialise le rapport en Markdown lisible (utile en revue)."""
    lines: List[str] = []
    lines.append(f"# Audit MATTE — issue #103 ({report.repo_root})")
    lines.append("")
    lines.append(f"- Table canonique de retrieval : `{report.canonical_table}`  ")
    lines.append(f"- Colonne canonique Albert / BGE-M3 : `{report.canonical_embed_col_albert}`  ")
    lines.append(f"- Colonne BGE-Scaleway : `{report.canonical_embed_col_bge}`  ")
    lines.append("")

    lines.append("## Notebooks")
    lines.append("")
    lines.append("| Chemin | Présent | Note |")
    lines.append("|---|---|---|")
    for n in report.notebooks:
        lines.append(f"| `{n.path}` | {'✅' if n.present else '❌'} | {n.note} |")
    lines.append("")

    if report.pdf_paths_declared:
        lines.append("## PDF déclarés dans `amelioration_matte.ipynb`")
        lines.append("")
        for p in report.pdf_paths_declared:
            lines.append(f"- `{p}`")
        lines.append("")

    lines.append("## Variables MATTE_ dans `.env.example`")
    lines.append("")
    lines.append("| Variable | Présent | Note |")
    lines.append("|---|---|---|")
    for v in report.env_vars:
        lines.append(f"| `{v.path}` | {'✅' if v.present else '❌'} | {v.note} |")
    lines.append("")

    if report.artifacts:
        lines.append("## Artefacts sous `data/out/` (lecture seule)")
        lines.append("")
        lines.append("| Chemin | Présent | Lignes | hash_id uniques | Texte vide | dim | Note |")
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for a in report.artifacts:
            lines.append(
                "| `{p}` | {ok} | {n} | {u} | {e} | {d} | {note} |".format(
                    p=a.path,
                    ok="✅" if a.present else "—",
                    n=a.row_count if a.row_count is not None else "—",
                    u=a.unique_hash_id if a.unique_hash_id is not None else "—",
                    e=a.empty_text if a.empty_text is not None else "—",
                    d=a.embedding_dim if a.embedding_dim is not None else "—",
                    note=a.note,
                )
            )
        lines.append("")

    if report.sql_statements:
        lines.append("## Requêtes SQL (read-only)")
        lines.append("")
        for s in report.sql_statements:
            lines.append(f"### `{s.name}` — {s.description}")
            lines.append("")
            lines.append("```sql")
            lines.append(s.sql)
            lines.append("```")
            lines.append("")

    if report.db_results:
        lines.append("## Résultats DB (read-only)")
        lines.append("")
        for name, payload in report.db_results.items():
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(payload, indent=2, default=str))
            lines.append("```")
            lines.append("")

    if report.diagnostics:
        lines.append("## Diagnostics")
        lines.append("")
        for d in report.diagnostics:
            lines.append(f"- {d}")
        lines.append("")

    if report.notes:
        lines.append("## Notes")
        lines.append("")
        for n in report.notes:
            lines.append(f"- {n}")
        lines.append("")

    if report.errors:
        lines.append("## Erreurs")
        lines.append("")
        for e in report.errors:
            lines.append(f"- {e}")
        lines.append("")

    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit_matte_ingestion.py",
        description=("Audit read-only/offline de l'ingestion source MATTE (issue #103). Voir docs/MATTE_SOURCE_INGESTION_AUDIT.md."),
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Chemin vers la racine du repo assistant-rh (défaut: .).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--sql-only",
        action="store_true",
        help=("Mode par défaut. Inspecte le repo + émet les requêtes SQL en stdout, sans connexion DB."),
    )
    mode.add_argument(
        "--db-readonly",
        action="store_true",
        help=(
            "Exécute les requêtes en lecture seule contre la base. "
            "Exige que la variable MATTE_AUDIT_DSN soit exportée. "
            "Aucune écriture n'est émise par construction."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Format de sortie (défaut: json).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.exists():
        print(f"❌ repo-root introuvable: {repo_root}", file=sys.stderr)
        return 2

    db_readonly = bool(args.db_readonly) and not args.sql_only
    report = build_report(repo_root, db_readonly=db_readonly)

    if args.format == "markdown":
        sys.stdout.write(render_markdown(report))
    else:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2, default=str))
    sys.stdout.write("\n")

    if report.errors:
        # On remonte un code non-zéro pour les erreurs graves (parse JSON du
        # notebook, etc.) mais on ne fail pas l'audit pour de simples
        # artefacts manquants : c'est un constat, pas une erreur.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
