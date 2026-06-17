"""Tests unitaires pour l'audit d'ingestion DGAFP/Légifrance (issue #102).

Couvre les trois corrections structurelles de l'audit :
- ingestion idempotente pour les embeddings (`_upsert(preserve_on_null_cols=...)`) ;
- mode audit read-only pour le backfill d'embeddings (`--check-only`) ;
- fail-fast par défaut sur extraction incomplète Légifrance
  (`--article-ids-json` + `--strict-articles` / `--allow-partial`).

Les tests n'ouvrent aucune connexion PostgreSQL réelle : on utilise des
fakes qui rendent ``as_string()`` / ``executemany()`` vérifiables. Aucune
dépendance aux modèles d'embedding, aucune API Scaleway, aucune base
réelle ne sont requises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# `assistant_rh_data_engineering.jobs` importe `psycopg` et `requests` au
# chargement ; `assistant_rh_data_engineering.service_public.db` importe
# `assistant_rh_shared`. Ces dépendances sont déjà installées via uv sync.
from assistant_rh_data_engineering.jobs import embeddings_backfill
from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Fakes psycopg
# ---------------------------------------------------------------------------


class _FakeColumnTypes:
    """Retourne ``{col: ('vector', None)}`` ou ``{'text': ('text', None)}``.

    Psycopg est principalement appelé pour :
    - ``information_schema.columns`` (mapping ``column_name`` -> ``(udt, len)``) ;
    - ``executemany(query, rows)`` ;
    - ``commit()``.

    On évite de charger psycopg.Column et on fournit juste ce qu'il faut à
    ``_column_types`` (un mapping).
    """

    def __init__(self, columns: dict[str, tuple[str, int | None]]):
        self._columns = columns

    def __call__(self, conn: "_FakeConnection", table: str) -> dict[str, tuple[str, int | None]]:
        return self._columns


def _build_information_schema_rows(
    columns: dict[str, tuple[str, int | None]],
) -> list[tuple[str, str, int | None]]:
    """Convertit ``{col: (udt, length)}`` en lignes ``information_schema``."""
    return [(col, udt, length) for col, (udt, length) in columns.items()]


class _FakeCursor:
    def __init__(self, owner: "_FakeConnection"):
        self.owner = owner
        self._description: list[tuple] | None = None

    @property
    def description(self) -> list[tuple] | None:
        return self._description

    def execute(self, query, params=None):
        # On stocke la query (objet) pour permettre aux tests d'appeler
        # ``as_string({})`` ou d'inspecter la composition.
        self.owner.executed_queries.append(query)
        self.owner.last_params = params
        # Si une valeur a été préparée via ``queue_fetchone``, elle est servie
        # à la prochaine ``fetchone()``. Pareil pour ``queue_fetchall``.
        if self.owner.queue_fetchone:
            self.owner.last_fetchone_value = self.owner.queue_fetchone.pop(0)
        if self.owner.queue_fetchall:
            self.owner.last_fetchall_value = self.owner.queue_fetchall.pop(0)

    def executemany(self, query, rows):
        self.owner.executed_queries.append(query)
        self.owner.last_executemany_rows = list(rows)
        self.owner.executemany_calls += 1

    def fetchone(self):
        return self.owner.last_fetchone_value

    def fetchall(self):
        return self.owner.last_fetchall_value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class _FakeConnection:
    def __init__(
        self,
        *,
        column_types: dict[str, tuple[str, int | None]] | None = None,
        fetchall_results: list | None = None,
        fetchone_results: list | None = None,
    ):
        self.executed_queries: list[str] = []
        self.executemany_calls = 0
        self.last_executemany_rows: list[dict] = []
        self.last_fetchone_value = None
        self.last_fetchall_value = None
        # Files FIFO : chaque ``cur.execute`` consomme la prochaine entrée
        # de la file pour peupler la valeur retournée par ``fetchone`` /
        # ``fetchall``.
        self.queue_fetchone: list = list(fetchone_results or [])
        self.queue_fetchall: list = list(fetchall_results or [])
        self.committed = False
        self._column_types = _FakeColumnTypes(column_types or {})
        self.rolled_back = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


# ---------------------------------------------------------------------------
# 1. Ingestion idempotente pour les embeddings (no-embed rerun safety)
# ---------------------------------------------------------------------------


def _render_query(upsert_sql) -> str:
    """Helper : rend un ``sql.SQL``/``sql.Composed`` en string Python lisible.

    Tolère aussi une ``str`` déjà rendue (cas défensif)."""
    if isinstance(upsert_sql, str):
        return upsert_sql
    return upsert_sql.as_string({})


def test_upsert_without_preserve_uses_plain_excluded() -> None:
    """Sanity check : sans ``preserve_on_null_cols``, le SQL généré utilise
    ``EXCLUDED.col`` (comportement historique inchangé)."""
    writer = ServicePublicDbWriter(schema="public")
    conn = _FakeConnection(
        column_types={
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
        },
        # ``_column_types`` lit ``information_schema.columns`` via fetchall.
        fetchall_results=[
            _build_information_schema_rows(
                {
                    "chunk_id": ("varchar", 64),
                    "chunk_text": ("text", None),
                    "embedding_m3": ("vector", None),
                    "embedding_bge_scw": ("vector", None),
                }
            )
        ],
    )
    rows = [
        {
            "chunk_id": "id1",
            "chunk_text": "hello",
            "embedding_m3": None,
            "embedding_bge_scw": None,
        }
    ]

    writer._upsert(conn, "rag_chunks_dgafp", rows, ["chunk_id"])

    assert conn.executed_queries, "executemany doit avoir été appelé"
    upsert_sql = _render_query(conn.executed_queries[-1]).replace("\n", " ")
    # psycopg rend les identifiants entre guillemets : on normalise.
    upsert_sql = upsert_sql.replace('"', "")
    assert "EXCLUDED.embedding_m3" in upsert_sql
    assert "COALESCE" not in upsert_sql


def test_upsert_with_preserve_on_null_uses_coalesce_for_embeddings() -> None:
    """Avec ``preserve_on_null_cols=['embedding_m3', 'embedding_bge_scw']``,
    l'UPDATE sur la branche conflict doit utiliser ``COALESCE(EXCLUDED.col,
    rag_chunks_dgafp.col)`` afin qu'un rerun --no-embed n'écrase pas les
    vecteurs déjà persistés à NULL.
    """
    writer = ServicePublicDbWriter(schema="public")
    conn = _FakeConnection(
        column_types={
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
        },
        fetchall_results=[
            _build_information_schema_rows(
                {
                    "chunk_id": ("varchar", 64),
                    "chunk_text": ("text", None),
                    "embedding_m3": ("vector", None),
                    "embedding_bge_scw": ("vector", None),
                }
            )
        ],
    )
    rows = [
        {
            "chunk_id": "id1",
            "chunk_text": "hello",
            "embedding_m3": None,
            "embedding_bge_scw": None,
        }
    ]

    writer._upsert(
        conn,
        "rag_chunks_dgafp",
        rows,
        ["chunk_id"],
        preserve_on_null_cols=["embedding_m3", "embedding_bge_scw"],
    )

    upsert_sql = _render_query(conn.executed_queries[-1]).replace("\n", " ")
    # psycopg rend les identifiants entre guillemets : on normalise.
    upsert_sql = upsert_sql.replace('"', "")
    assert "COALESCE(EXCLUDED.embedding_m3, rag_chunks_dgafp.embedding_m3)" in upsert_sql
    assert "COALESCE(EXCLUDED.embedding_bge_scw, rag_chunks_dgafp.embedding_bge_scw)" in upsert_sql
    # Les colonnes non embedding ne sont pas affectées.
    assert "COALESCE(EXCLUDED.chunk_text" not in upsert_sql


def test_upsert_preserve_on_null_unknown_columns_are_ignored() -> None:
    """Si ``preserve_on_null_cols`` mentionne une colonne absente du
    schéma ou de l'assignation (typiquement hors ``assignments`` parce
    qu'elle est en ``conflict_cols``), on n'émet pas de COALESCE."""
    writer = ServicePublicDbWriter(schema="public")
    conn = _FakeConnection(
        column_types={
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
        },
        fetchall_results=[
            _build_information_schema_rows(
                {
                    "chunk_id": ("varchar", 64),
                    "chunk_text": ("text", None),
                    "embedding_m3": ("vector", None),
                }
            )
        ],
    )
    rows = [
        {
            "chunk_id": "id1",
            "chunk_text": "hello",
            "embedding_m3": None,
        }
    ]

    writer._upsert(
        conn,
        "rag_chunks_dgafp",
        rows,
        ["chunk_id"],
        preserve_on_null_cols=["embedding_m3", "embedding_does_not_exist"],
    )

    upsert_sql = _render_query(conn.executed_queries[-1]).replace("\n", " ")
    # psycopg rend les identifiants entre guillemets : on normalise.
    upsert_sql = upsert_sql.replace('"', "")
    assert "COALESCE(EXCLUDED.embedding_m3" in upsert_sql
    assert "embedding_does_not_exist" not in upsert_sql


def test_legifrance_upsert_legacy_chunks_preserves_existing_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le writer Légifrance legacy doit préserver les embeddings
    ``embedding_m3`` / ``embedding_bge_scw`` / ``embedding_qwen3`` déjà
    persistés lorsque le run d'ingestion est en mode --no-embed."""
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter

    captured: dict[str, object] = {}
    fake_conn = _FakeConnection(
        column_types={
            "chunk_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
            "embedding_qwen3": ("vector", None),
        },
        fetchall_results=[
            _build_information_schema_rows(
                {
                    "chunk_id": ("varchar", 64),
                    "chunk_text": ("text", None),
                    "embedding_m3": ("vector", None),
                    "embedding_bge_scw": ("vector", None),
                    "embedding_qwen3": ("vector", None),
                }
            )
        ],
    )

    def fake_connect(self):  # noqa: ARG001
        return fake_conn

    def fake_upsert(self, conn, table, rows, conflict_cols, **kwargs):
        captured["table"] = table
        captured["conflict_cols"] = conflict_cols
        captured["preserve_on_null_cols"] = list(kwargs.get("preserve_on_null_cols") or [])
        captured["rows"] = list(rows)
        return len(rows)

    monkeypatch.setattr(LegifranceDbWriter, "_connect", fake_connect)
    monkeypatch.setattr(LegifranceDbWriter, "_upsert", fake_upsert)
    try:
        writer = LegifranceDbWriter()
        chunks = [
            {
                "chunk_id": "LEGIARTI000006207978_0",
                "chunk_text": "hello",
                "embedding_m3": None,
                "embedding_bge_scw": None,
                "embedding_qwen3": None,
                "_targets": ["legacy"],
            }
        ]
        count = writer.upsert_legacy_chunks(chunks)
    finally:
        # ``monkeypatch`` restore automatique.
        pass

    assert count == 1
    assert captured["table"] == "rag_chunks_dgafp"
    assert captured["conflict_cols"] == ["chunk_id"]
    preserve = captured["preserve_on_null_cols"]
    assert "embedding_m3" in preserve
    assert "embedding_bge_scw" in preserve
    assert "embedding_qwen3" in preserve


def test_legifrance_upsert_modern_chunks_preserves_existing_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le writer Légifrance moderne doit préserver les embeddings
    ``embedding_m3`` / ``embedding_bge_scw`` (pas de qwen3 sur la table
    moderne)."""
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter

    captured: dict[str, object] = {}
    fake_conn = _FakeConnection(
        column_types={
            "hash_id": ("varchar", 64),
            "chunk_text": ("text", None),
            "embedding_m3": ("vector", None),
            "embedding_bge_scw": ("vector", None),
        },
        fetchall_results=[
            _build_information_schema_rows(
                {
                    "hash_id": ("varchar", 64),
                    "chunk_text": ("text", None),
                    "embedding_m3": ("vector", None),
                    "embedding_bge_scw": ("vector", None),
                }
            )
        ],
    )

    def fake_connect(self):  # noqa: ARG001
        return fake_conn

    def fake_upsert(self, conn, table, rows, conflict_cols, **kwargs):
        captured["table"] = table
        captured["conflict_cols"] = conflict_cols
        captured["preserve_on_null_cols"] = list(kwargs.get("preserve_on_null_cols") or [])
        return len(rows)

    monkeypatch.setattr(LegifranceDbWriter, "_connect", fake_connect)
    monkeypatch.setattr(LegifranceDbWriter, "_upsert", fake_upsert)
    try:
        writer = LegifranceDbWriter()
        chunks = [
            {
                "hash_id": "hash-1",
                "chunk_text": "hello",
                "embedding_m3": None,
                "embedding_bge_scw": None,
                "_targets": ["modern"],
            }
        ]
        count = writer.upsert_modern_chunks(chunks)
    finally:
        pass

    assert count == 1
    assert captured["table"] == "rag_chunks_legifrance"
    assert captured["conflict_cols"] == ["hash_id"]
    preserve = captured["preserve_on_null_cols"]
    assert "embedding_m3" in preserve
    assert "embedding_bge_scw" in preserve
    assert "embedding_qwen3" not in preserve


# ---------------------------------------------------------------------------
# 2. Mode audit read-only (--check-only / --dry-run)
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, tables: list[dict]) -> Path:
    path = tmp_path / "embeddings.json"
    path.write_text(json.dumps({"tables": tables}), encoding="utf-8")
    return path


def test_filter_table_specs_applies_only_table_and_only_column() -> None:
    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [
                {"column": "embedding_m3", "algorithm": "m3"},
                {"column": "embedding_bge_scw", "algorithm": "bge_scaleway"},
            ],
        },
        {
            "table": "rag_chunks_legifrance",
            "id_column": "hash_id",
            "text_column": "chunk_text",
            "embeddings": [
                {"column": "embedding_m3", "algorithm": "m3"},
            ],
        },
    ]

    only_dgafp = embeddings_backfill._filter_table_specs(specs, only_table="rag_chunks_dgafp", only_column=None)
    assert [spec["table"] for spec in only_dgafp] == ["rag_chunks_dgafp"]

    only_column = embeddings_backfill._filter_table_specs(specs, only_table=None, only_column="embedding_m3")
    assert [spec["table"] for spec in only_column] == [
        "rag_chunks_dgafp",
        "rag_chunks_legifrance",
    ]
    assert all(len(spec["embeddings"]) == 1 for spec in only_column)
    assert all(spec["embeddings"][0]["column"] == "embedding_m3" for spec in only_column)

    both = embeddings_backfill._filter_table_specs(specs, only_table="rag_chunks_dgafp", only_column="embedding_bge_scw")
    assert len(both) == 1
    assert both[0]["embeddings"][0]["column"] == "embedding_bge_scw"


def test_audit_embedding_coverage_reports_per_column_stats_without_writes() -> None:
    conn = _FakeConnection(
        fetchone_results=[
            (1,),  # table existe
            (10, 4, 5, 1),  # stats embedding_m3
        ]
    )

    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
        }
    ]

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", specs)

    assert report["schema"] == "public"
    assert report["missing_tables"] == []
    stats = report["tables"]["rag_chunks_dgafp"]["embedding_m3"]
    assert stats["total"] == 10
    assert stats["non_null"] == 4
    assert stats["missing_with_text"] == 5
    assert stats["empty_text"] == 1
    assert stats["coverage_pct"] == 40.0
    # Le mode audit n'écrit jamais.
    assert conn.executemany_calls == 0


def test_audit_embedding_coverage_marks_missing_tables() -> None:
    conn = _FakeConnection(
        # SELECT 1 FROM information_schema.tables : aucune ligne => fetchone None.
        fetchone_results=[None]
    )

    specs = [
        {
            "table": "rag_chunks_dgafp",
            "id_column": "chunk_id",
            "text_column": "chunk_text",
            "embeddings": [{"column": "embedding_m3", "algorithm": "m3"}],
        }
    ]

    report = embeddings_backfill.audit_embedding_coverage(conn, "public", specs)
    assert report["missing_tables"] == ["rag_chunks_dgafp"]
    assert report["tables"] == {}


def test_evaluate_coverage_report_threshold_violation_returns_one() -> None:
    report = {
        "schema": "public",
        "tables": {
            "rag_chunks_dgafp": {
                "embedding_m3": {
                    "total": 100,
                    "non_null": 80,
                    "missing_with_text": 20,
                    "empty_text": 0,
                    "coverage_pct": 80.0,
                }
            }
        },
        "missing_tables": [],
    }
    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=100.0)
    assert exit_code == 1
    assert any("rag_chunks_dgafp.embedding_m3" in problem for problem in problems)


def test_evaluate_coverage_report_full_coverage_returns_zero() -> None:
    report = {
        "schema": "public",
        "tables": {
            "rag_chunks_dgafp": {
                "embedding_m3": {
                    "total": 100,
                    "non_null": 100,
                    "missing_with_text": 0,
                    "empty_text": 0,
                    "coverage_pct": 100.0,
                }
            }
        },
        "missing_tables": [],
    }
    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=100.0)
    assert exit_code == 0
    assert problems == []


def test_evaluate_coverage_report_missing_table_returns_one() -> None:
    report = {
        "schema": "public",
        "tables": {},
        "missing_tables": ["rag_chunks_dgafp"],
    }
    exit_code, problems = embeddings_backfill.evaluate_coverage_report(report, coverage_min_pct=100.0)
    assert exit_code == 1
    assert problems == ["Table absente: rag_chunks_dgafp"]


def test_check_only_main_does_not_import_models_or_call_apis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Le mode ``--check-only`` doit :
    - ouvrir une connexion psycopg ;
    - appeler ``audit_embedding_coverage`` ;
    - **ne pas** importer ``sentence_transformers`` ;
    - **ne pas** créer de ``ScalewayBgeClient`` ;
    - **ne pas** exécuter ``update_embeddings`` ;
    - retourner un code de sortie cohérent avec la couverture observée.
    """
    config_path = _make_config(
        tmp_path,
        tables=[
            {
                "table": "rag_chunks_dgafp",
                "id_column": "chunk_id",
                "text_column": "chunk_text",
                "embeddings": [
                    {"column": "embedding_m3", "algorithm": "m3"},
                    {"column": "embedding_bge_scw", "algorithm": "bge_scaleway"},
                ],
            }
        ],
    )

    fake_conn = _FakeConnection(
        fetchone_results=[
            (1,),  # table existe (1 seule table : rag_chunks_dgafp)
            (10, 0, 10, 0),  # stats embedding_m3
            (10, 0, 10, 0),  # stats embedding_bge_scw
        ]
    )

    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://placeholder")
    monkeypatch.setattr(
        embeddings_backfill,
        "psycopg",
        SimpleNamespace(connect=lambda dsn: fake_conn),
    )

    sentence_transformers = MagicMock()
    sentence_transformers.SentenceTransformer = MagicMock(
        side_effect=AssertionError("SentenceTransformer ne doit PAS être importé en mode --check-only")
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)

    bge_client_constructor = MagicMock(side_effect=AssertionError("ScalewayBgeClient ne doit PAS être instancié en mode --check-only"))
    monkeypatch.setattr(embeddings_backfill, "ScalewayBgeClient", bge_client_constructor)

    update_embeddings_called = MagicMock(side_effect=AssertionError("update_embeddings ne doit PAS être appelé en mode --check-only"))
    monkeypatch.setattr(embeddings_backfill, "update_embeddings", update_embeddings_called)

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion embeddings legifrance",
            "--config",
            str(config_path),
            "--check-only",
            "--coverage-min-pct",
            "100",
        ],
    )

    exit_code = embeddings_backfill.main()

    # Couverture 0% < seuil 100 => exit 1.
    assert exit_code == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["check_only"] is True
    assert payload["only_table"] is None
    assert payload["only_column"] is None
    assert payload["exit_code"] == 1
    assert payload["problems"], "Au moins un problème doit être listé"
    assert any("rag_chunks_dgafp.embedding_m3" in problem for problem in payload["problems"])

    sentence_transformers.SentenceTransformer.assert_not_called()
    bge_client_constructor.assert_not_called()
    update_embeddings_called.assert_not_called()


def test_normal_backfill_main_initializes_tables_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Régression : sans ``--check-only``, ``main()`` écrit
    ``summary["tables"][table] = ...`` à la fin de chaque table. La clé
    ``"tables"`` doit donc être initialisée à ``{}`` dans le summary,
    sinon le run normal lève ``KeyError`` à la première table traitée.
    """
    config_path = _make_config(
        tmp_path,
        tables=[
            {
                "table": "rag_chunks_dgafp",
                "id_column": "chunk_id",
                "text_column": "chunk_text",
                "embeddings": [
                    {"column": "embedding_m3", "algorithm": "m3"},
                    {"column": "embedding_bge_scw", "algorithm": "bge_scaleway"},
                ],
            }
        ],
    )

    fake_conn = _FakeConnection()

    monkeypatch.setenv("SCW_POSTGRES_DSN", "postgresql://placeholder")
    monkeypatch.setattr(
        embeddings_backfill,
        "psycopg",
        SimpleNamespace(connect=lambda dsn: fake_conn),
    )

    monkeypatch.setattr(embeddings_backfill, "backfill_m3", lambda *args, **kwargs: 0)
    monkeypatch.setattr(embeddings_backfill, "backfill_bge_scaleway", lambda *args, **kwargs: 0)

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion embeddings legifrance",
            "--config",
            str(config_path),
        ],
    )

    exit_code = embeddings_backfill.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["check_only"] is False
    assert payload["tables"] == {"rag_chunks_dgafp": {"embedding_m3": 0, "embedding_bge_scw": 0}}


# ---------------------------------------------------------------------------
# 3. Fail-fast Légifrance sur extraction incomplète
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_bulk_dump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub des imports réseau / object-storage du job bulk-dump."""

    class _StubClient:
        def __init__(self, config):
            self.config = config

        def resolve_snapshot(self, raw_dir):
            return SimpleNamespace(
                archive_url="https://example/test.tar.gz",
                archive_name="test.tar.gz",
                archive_path=tmp_path / "test.tar.gz",
                extract_dir=tmp_path / "extract",
                index_path=None,
            )

        def extract_articles(self, snapshot, article_ids):
            return {aid: SimpleNamespace() for aid in article_ids if not aid.startswith("MISSING_")}

        def extract_full_snapshot(self, snapshot):
            return {}

        def delete_local_archive(self, snapshot):
            return False

    # Stub le module ``assistant_rh_data_engineering.legifrance.bulk_dump``
    # où ``main()`` importe ``LegiBulkDumpClient``.
    import assistant_rh_data_engineering.legifrance.bulk_dump as bulk_dump_module

    monkeypatch.setattr(bulk_dump_module, "LegiBulkDumpClient", _StubClient)

    class _StubSync:
        def sync_medallion_root(self, *args, **kwargs):
            return None

    import assistant_rh_data_engineering.utils.object_storage as object_storage_module

    monkeypatch.setattr(object_storage_module, "ScalewayObjectStorageSync", lambda config: _StubSync())


def _write_manifest(tmp_path: Path, article_ids: list[str]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"article_cids": article_ids}), encoding="utf-8")
    return path


def test_bulk_dump_strict_by_default_fails_on_missing_ids(
    tmp_path: Path,
    stub_bulk_dump: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = _write_manifest(
        tmp_path,
        ["LEGIARTI000006207978", "MISSING_LEGIARTI0000"],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion legifrance bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        legifrance_bulk_dump.main()

    message = str(exc_info.value)
    assert "incomplète" in message
    assert "MISSING_LEGIARTI0000" in message

    # Le job imprime aussi un payload JSON avec le code raison.
    captured = capsys.readouterr()
    # Le payload est multi-ligne indenté : on décode l'objet complet.
    decoded = json.loads(captured.out.strip())
    assert decoded.get("status") == "error"
    assert decoded["reason"] == "incomplete_article_extraction"
    assert decoded["requested_count"] == 2
    assert decoded["extracted_xml_count"] == 1
    assert decoded["missing_count"] == 1


def test_bulk_dump_allow_partial_succeeds_on_missing_ids(
    tmp_path: Path,
    stub_bulk_dump: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = _write_manifest(
        tmp_path,
        ["LEGIARTI000006207978", "MISSING_LEGIARTI0000"],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion legifrance bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
            "--allow-partial",
        ],
    )

    exit_code = legifrance_bulk_dump.main()

    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 1
    assert payload["missing_article_count"] == 1
    assert payload["missing_article_ids"] == ["MISSING_LEGIARTI0000"]
    assert payload["strict_articles"] is False


def test_bulk_dump_strict_explicit_succeeds_when_all_ids_found(
    tmp_path: Path,
    stub_bulk_dump: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    manifest = _write_manifest(
        tmp_path,
        ["LEGIARTI000006207978", "LEGIARTI000006207979"],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion legifrance bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--article-ids-json",
            str(manifest),
            "--strict-articles",
        ],
    )

    exit_code = legifrance_bulk_dump.main()

    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["requested_article_ids"] == 2
    assert payload["extracted_xml_count"] == 2
    assert payload["missing_article_count"] == 0
    assert payload["strict_articles"] is True


def test_bulk_dump_no_article_ids_keeps_legacy_behavior(
    tmp_path: Path,
    stub_bulk_dump: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sans ``--article-ids-json``, pas de fail-fast (le mode
    ``extract_full_snapshot`` n'a pas de notion d'IDs à valider)."""
    from assistant_rh_data_engineering.jobs import legifrance_bulk_dump

    monkeypatch.setattr(
        "sys.argv",
        [
            "data-ingestion legifrance bulk-dump",
            "--lake-root",
            str(tmp_path / "lake"),
            "--extract-full-snapshot",
        ],
    )

    exit_code = legifrance_bulk_dump.main()

    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["extraction_mode"] == "full_snapshot"
    assert payload["article_ids_json"] is None
    assert payload["strict_articles"] is False
