"""Prepare private goldsets and resolve source labels against RAG corpus tables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

DEFAULT_PRIVATE_GOLDSET_REPO = "DGAFP/assistant-rh-private-data"
DEFAULT_PRIVATE_GOLDSET_NAME = "priority_contractuels_v1"
DEFAULT_PRIVATE_GOLDSET_SUBDIR_TEMPLATE = "goldsets/{goldset_name}"
DEFAULT_CACHE_DIR = Path(".cache") / "assistant-rh" / "goldsets"

RAW_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "question": ("Questions", "Question", "question", "questions"),
    "gold_answer": ("Réponses", "Reponses", "Réponse", "Reponse", "gold_answer", "answer"),
    "theme": ("Thématique", "Thematique", "Theme", "theme"),
    "sources": ("Sources", "Source", "sources", "source"),
    "keywords": ("Mots-clés", "Mots-cles", "Mots clés", "Mots cles", "keywords", "tags"),
    "ministere": ("Ministère", "Ministere", "Ministry", "ministere"),
}

ENRICHED_COLUMNS = [
    "id",
    "question",
    "gold_answer",
    "theme",
    "goldset_name",
    "tags",
    "ministere",
    "source_labels",
    "gold_sources",
    "gold_source_links",
    "gold_chunk_ids",
    "gold_section_ids",
    "link_status",
    "link_warnings",
]

SOURCE_LINK_COLUMNS = [
    "goldset_name",
    "row_id",
    "source_label",
    "source_kind",
    "status",
    "score",
    "publisher",
    "doc_id",
    "doc_short_id",
    "doc_title",
    "section_id",
    "section_heading",
    "chunk_table",
    "chunk_id",
    "warning",
]

SOURCE_START_RE = re.compile(
    r"(?=\b(?:Fiche\s+(?:MATTE|SP|MSO)|MATTE\s*:|Article\s+[A-ZLDR0-9]|Décret\s+n|Decret\s+n|Arrêté\s+du|Arrete\s+du|Fiche\s+Service|Fiche\s+SP|Fiche\s+MSO)\b)",
    re.IGNORECASE,
)

LEGAL_SOURCE_RE = re.compile(r"\b(article|décret|decret|arrêté|arrete|code général|code du travail|cgfp)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RawGoldsetRow:
    row_id: str
    question: str
    gold_answer: str
    theme: str
    sources: str
    keywords: list[str]
    ministere: str
    goldset_name: str

    def with_extra_tags(self, extra_tags: Sequence[str]) -> "RawGoldsetRow":
        return RawGoldsetRow(
            row_id=self.row_id,
            question=self.question,
            gold_answer=self.gold_answer,
            theme=self.theme,
            sources=self.sources,
            keywords=_unique_preserve_order([*self.keywords, *extra_tags]),
            ministere=self.ministere,
            goldset_name=self.goldset_name,
        )


@dataclass
class SourceLink:
    source_label: str
    source_kind: str
    status: str
    score: float = 0.0
    publisher: str = ""
    doc_id: str = ""
    doc_short_id: str = ""
    doc_title: str = ""
    section_id: str = ""
    section_heading: str = ""
    chunk_table: str = ""
    chunk_id: str = ""
    warning: str = ""

    def compact(self) -> dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v not in ("", None, [], {})}


@dataclass
class PreparedRow:
    raw: RawGoldsetRow
    source_labels: list[str]
    links: list[SourceLink] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def link_status(self) -> str:
        if not self.source_labels:
            return "unresolved"
        statuses = [link.status for link in self.links]
        if not statuses:
            return "unresolved"
        if all(status == "resolved" for status in statuses) and len(statuses) >= len(self.source_labels):
            return "resolved"
        if any(status == "ambiguous" for status in statuses):
            return "ambiguous"
        if any(status == "resolved" for status in statuses):
            return "partial"
        if any(status == "partial" for status in statuses):
            return "partial"
        return "unresolved"

    def gold_sources(self) -> list[str]:
        return _unique_preserve_order(link.doc_short_id for link in self.links if link.doc_short_id)

    def gold_chunk_ids(self) -> list[str]:
        return _unique_preserve_order(link.chunk_id for link in self.links if link.chunk_id)

    def gold_section_ids(self) -> list[str]:
        return _unique_preserve_order(link.section_id for link in self.links if link.section_id)

    def to_enriched_dict(self) -> dict[str, str]:
        link_warnings = list(self.warnings)
        link_warnings.extend(link.warning for link in self.links if link.warning)
        return {
            "id": self.raw.row_id,
            "question": self.raw.question,
            "gold_answer": self.raw.gold_answer,
            "theme": self.raw.theme,
            "goldset_name": self.raw.goldset_name,
            "tags": json.dumps(self.raw.keywords, ensure_ascii=False),
            "ministere": self.raw.ministere,
            "source_labels": json.dumps(self.source_labels, ensure_ascii=False),
            "gold_sources": json.dumps(self.gold_sources(), ensure_ascii=False),
            "gold_source_links": json.dumps([link.compact() for link in self.links], ensure_ascii=False),
            "gold_chunk_ids": json.dumps(self.gold_chunk_ids(), ensure_ascii=False),
            "gold_section_ids": json.dumps(self.gold_section_ids(), ensure_ascii=False),
            "link_status": self.link_status,
            "link_warnings": json.dumps(_unique_preserve_order(link_warnings), ensure_ascii=False),
        }


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def tokenize(value: str) -> set[str]:
    stopwords = {
        "a",
        "au",
        "aux",
        "de",
        "des",
        "du",
        "et",
        "la",
        "le",
        "les",
        "un",
        "une",
        "dans",
        "pour",
        "sur",
        "fiche",
        "article",
        "juillet",
        "code",
        "general",
        "fonction",
        "publique",
    }
    return {token for token in normalize_text(value).split() if len(token) > 1 and token not in stopwords}


def score_text_match(query: str, candidate: str) -> float:
    query_norm = normalize_text(query)
    candidate_norm = normalize_text(candidate)
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0
    if query_norm in candidate_norm or candidate_norm in query_norm:
        return 0.92

    query_tokens = tokenize(query_norm)
    candidate_tokens = tokenize(candidate_norm)
    token_score = 0.0
    if query_tokens and candidate_tokens:
        overlap = len(query_tokens & candidate_tokens)
        token_score = (0.7 * (overlap / len(query_tokens))) + (0.3 * (overlap / len(candidate_tokens)))

    fuzzy = SequenceMatcher(None, query_norm, candidate_norm).ratio()
    return max(token_score, fuzzy * 0.85)


def split_keywords(raw_keywords: str) -> list[str]:
    if not raw_keywords:
        return []
    parts = re.split(r"[,;\n]+", raw_keywords)
    return _unique_preserve_order(part.strip() for part in parts if part.strip())


def split_source_labels(raw_sources: str) -> list[str]:
    if not raw_sources:
        return []

    normalized = re.sub(r"[\r\n]+", " ; ", raw_sources.strip())
    coarse_parts = [part.strip(" ;") for part in re.split(r"\s+;\s+|[\t]+", normalized) if part.strip(" ;")]
    out: list[str] = []
    for part in coarse_parts:
        starts = [match.start() for match in SOURCE_START_RE.finditer(part)]
        if not starts or starts[0] != 0:
            starts = [0] + starts
        starts = sorted(set(starts))
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(part)
            label = part[start:end].strip(" ;")
            if label:
                out.append(label)
    return _unique_preserve_order(out)


def classify_source_label(label: str, ministere: str = "") -> str:
    normalized = normalize_text(label)
    ministry = normalize_text(ministere)
    if LEGAL_SOURCE_RE.search(label):
        return "legal"
    if "service public" in normalized or re.search(r"\bsp\b", normalized):
        return "service_public"
    if "mso" in normalized or ministry == "mso":
        return "mso"
    if "matte" in normalized or ministry == "matte":
        return "matte"
    if "rgrh" in normalized:
        return "rgrh"
    return "document"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Input file not found: {path}")
    sample = path.read_text(encoding="utf-8-sig")[:4096]
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in first_line else csv.excel
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, dialect=dialect))


def resolve_column_map(fieldnames: Sequence[str], overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    available = {normalize_text(name): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for canonical, aliases in RAW_COLUMN_ALIASES.items():
        if overrides and overrides.get(canonical):
            override = overrides[canonical]
            if override not in fieldnames:
                raise ValueError(f"Overridden column '{override}' for '{canonical}' not found in CSV fields: {list(fieldnames)}")
            mapping[canonical] = override
            continue
        for alias in aliases:
            key = normalize_text(alias)
            if key in available:
                mapping[canonical] = available[key]
                break
    missing = [key for key in RAW_COLUMN_ALIASES if key not in mapping]
    if missing:
        raise ValueError(f"Missing required raw goldset columns: {', '.join(missing)}")
    return mapping


def parse_raw_rows(
    rows: list[dict[str, str]],
    *,
    goldset_name: str,
    column_overrides: Mapping[str, str] | None = None,
) -> list[RawGoldsetRow]:
    if not rows:
        raise ValueError("Input goldset is empty")

    mapping = resolve_column_map(list(rows[0].keys()), column_overrides)
    parsed: list[RawGoldsetRow] = []
    for idx, row in enumerate(rows, start=1):
        question = _cell(row, mapping["question"])
        answer = _cell(row, mapping["gold_answer"])
        theme = _cell(row, mapping["theme"])
        sources = _cell(row, mapping["sources"])
        ministere = _cell(row, mapping["ministere"]) or infer_ministere_from_sources(sources)
        keywords = split_keywords(_cell(row, mapping["keywords"]))
        row_id = stable_row_id(question, fallback=f"q{idx:03d}")
        parsed.append(
            RawGoldsetRow(
                row_id=row_id,
                question=question,
                gold_answer=answer,
                theme=theme,
                sources=sources,
                keywords=keywords,
                ministere=ministere,
                goldset_name=goldset_name,
            )
        )
    return parsed


def infer_ministere_from_sources(sources: str) -> str:
    labels = split_source_labels(sources)
    kinds = [classify_source_label(label, "") for label in labels]
    if "mso" in kinds:
        return "MSO"
    if "matte" in kinds:
        return "MATTE"
    if "service_public" in kinds:
        return "Service-Public"
    if "legal" in kinds:
        return "DGAFP"
    if "rgrh" in kinds:
        return "RGRH"
    return "unknown"


def validate_raw_rows(rows: Iterable[RawGoldsetRow]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        if not row.question:
            errors.append(f"{row.row_id}: missing question")
        if not row.gold_answer:
            errors.append(f"{row.row_id}: missing answer")
        if not row.theme:
            errors.append(f"{row.row_id}: missing theme")
        if not row.ministere:
            errors.append(f"{row.row_id}: missing ministry")
        if not row.goldset_name:
            errors.append(f"{row.row_id}: missing goldset name")
    return errors


def validate_enriched_rows(rows: list[dict[str, str]]) -> list[str]:
    if not rows:
        return ["Enriched goldset is empty"]
    missing = [col for col in ENRICHED_COLUMNS if col not in rows[0]]
    if missing:
        return [f"Missing enriched columns: {', '.join(missing)}"]

    errors: list[str] = []
    for idx, row in enumerate(rows, start=1):
        row_id = row.get("id") or f"line {idx}"
        for col in ("question", "gold_answer", "theme", "ministere", "goldset_name"):
            if not str(row.get(col, "") or "").strip():
                errors.append(f"{row_id}: missing {col}")
        for col in ("tags", "source_labels", "gold_sources", "gold_source_links", "gold_chunk_ids", "gold_section_ids", "link_warnings"):
            try:
                parsed = json.loads(row.get(col, "") or "[]")
            except json.JSONDecodeError:
                errors.append(f"{row_id}: {col} is not valid JSON")
                continue
            if not isinstance(parsed, list):
                errors.append(f"{row_id}: {col} must be a JSON list")
        status = str(row.get("link_status", "") or "")
        if status not in {"resolved", "partial", "ambiguous", "unresolved"}:
            errors.append(f"{row_id}: invalid link_status {status!r}")
    return errors


def stable_row_id(question: str, *, fallback: str) -> str:
    normalized = normalize_text(question)
    if not normalized:
        return fallback
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"q_{digest}"


class GoldsetResolver:
    """Resolve source labels to document, section, and chunk identifiers."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: psycopg.Connection[Any] | None = None

    def resolve_row(self, row: RawGoldsetRow) -> PreparedRow:
        labels = split_source_labels(row.sources)
        prepared = PreparedRow(raw=row, source_labels=labels)
        for label in labels:
            prepared.links.extend(self.resolve_source(label, row))
        return prepared

    def resolve_source(self, label: str, row: RawGoldsetRow) -> list[SourceLink]:
        source_kind = classify_source_label(label, row.ministere)
        if source_kind == "legal":
            links = self.resolve_legal_source(label)
            if links:
                return links

        candidates = self.find_document_candidates(label, source_kind, row.ministere)
        if not candidates:
            return [
                SourceLink(
                    source_label=label,
                    source_kind=source_kind,
                    status="unresolved",
                    warning="No matching rag_documents or source_name candidate found.",
                )
            ]

        ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)
        best = ranked[0]
        ambiguous = len(ranked) > 1 and best["score"] >= 0.35 and ranked[1]["score"] >= best["score"] - 0.05
        link = self.build_document_link(label, source_kind, row, best)
        if ambiguous:
            link.status = "ambiguous"
            link.warning = "Ambiguous document candidates: " + "; ".join(
                f"{candidate.get('short_id') or '-'} {candidate.get('title') or candidate.get('full_title') or '-'} ({candidate['score']:.2f})"
                for candidate in ranked[:3]
            )
        return [link]

    def resolve_legal_source(self, label: str) -> list[SourceLink]:
        sql = """
            SELECT chunk_id, title, full_title, number, category, url, cid
            FROM public.rag_chunks_dgafp
            WHERE
                lower(coalesce(number, '')) = lower(%s)
                OR lower(coalesce(title, '')) LIKE lower(%s)
                OR lower(coalesce(full_title, '')) LIKE lower(%s)
                OR lower(coalesce(cid, '')) = lower(%s)
            LIMIT 20
        """
        reference = _extract_legal_reference(label)
        like = f"%{reference or label}%"
        rows = self.fetchall(sql, (reference or label, like, like, reference or label))
        if not rows:
            return []
        scored = []
        for row in rows:
            haystack = " ".join(str(row.get(key) or "") for key in ("number", "title", "full_title", "category", "cid"))
            scored.append((score_text_match(label, haystack), row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        score, best = scored[0]
        return [
            SourceLink(
                source_label=label,
                source_kind="legal",
                status="resolved" if score >= 0.35 else "partial",
                score=round(score, 4),
                publisher="DGAFP",
                doc_short_id=str(best.get("number") or best.get("cid") or ""),
                doc_title=str(best.get("full_title") or best.get("title") or ""),
                chunk_table="rag_chunks_dgafp",
                chunk_id=str(best.get("chunk_id") or ""),
                warning="" if score >= 0.35 else "Low-confidence legal match.",
            )
        ]

    def find_document_candidates(self, label: str, source_kind: str, ministere: str) -> list[dict[str, Any]]:
        publisher = preferred_publisher(source_kind, ministere)
        tokens = sorted(tokenize(label))
        ilike_terms = [f"%{token}%" for token in tokens[:6]]
        where_parts = [
            "lower(coalesce(d.title, '')) LIKE lower(%s)",
            "lower(coalesce(d.full_title, '')) LIKE lower(%s)",
            "lower(coalesce(d.short_id, '')) LIKE lower(%s)",
        ]
        params: list[Any] = [f"%{label}%", f"%{label}%", f"%{label}%"]
        for term in ilike_terms:
            where_parts.append("lower(coalesce(d.title, '') || ' ' || coalesce(d.full_title, '')) LIKE lower(%s)")
            params.append(term)
        publisher_sql = ""
        if publisher:
            publisher_sql = "ORDER BY CASE WHEN lower(coalesce(d.publisher, '')) = lower(%s) THEN 0 ELSE 1 END, d.updated_at DESC NULLS LAST"
            params.append(publisher)
        else:
            publisher_sql = "ORDER BY d.updated_at DESC NULLS LAST"

        sql = f"""
            SELECT d.doc_id, d.short_id, d.title, d.full_title, d.publisher, d.source_url
            FROM public.rag_documents d
            WHERE {" OR ".join(where_parts)}
            {publisher_sql}
            LIMIT 50
        """
        rows = self.fetchall(sql, tuple(params))

        if len(rows) < 5:
            rows.extend(self.find_source_name_candidates(label, publisher))

        scored: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = str(row.get("doc_id") or row.get("source_document_id") or "")
            key = doc_id or str(row.get("short_id") or row.get("title") or "")
            if not key:
                continue
            title_blob = " ".join(str(row.get(k) or "") for k in ("short_id", "title", "full_title", "source_name"))
            score = score_text_match(label, title_blob)
            row_publisher = str(row.get("publisher") or row.get("source") or "")
            if publisher and normalize_text(row_publisher) == normalize_text(publisher):
                score = min(score + 0.08, 1.0)
            if source_kind == "mso" and ("mso" in normalize_text(title_blob) or normalize_text(row_publisher) == "mso"):
                score = min(score + 0.06, 1.0)
            candidate = {**row, "score": round(score, 4)}
            if key not in scored or candidate["score"] > scored[key]["score"]:
                scored[key] = candidate
        return [candidate for candidate in scored.values() if candidate["score"] >= 0.2]

    def find_source_name_candidates(self, label: str, publisher: str | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table in ("rag_chunks_matte", "rag_chunks_service_public", "rag_chunks_rgrh"):
            sql = f"""
                SELECT DISTINCT
                    d.doc_id,
                    coalesce(d.short_id, t.short_id) AS short_id,
                    coalesce(d.title, t.source_name) AS title,
                    d.full_title,
                    coalesce(d.publisher, %s) AS publisher,
                    d.source_url,
                    t.source_name
                FROM public.{table} t
                LEFT JOIN public.rag_documents d ON d.doc_id = t.source_document_id OR d.short_id = t.short_id
                WHERE lower(coalesce(t.source_name, '')) LIKE lower(%s)
                LIMIT 20
            """
            try:
                rows.extend(self.fetchall(sql, (publisher or table, f"%{label}%")))
            except Exception:
                continue
        return rows

    def build_document_link(
        self,
        label: str,
        source_kind: str,
        row: RawGoldsetRow,
        document: dict[str, Any],
    ) -> SourceLink:
        section = self.find_best_section(document, label, row)
        chunk = self.find_best_chunk(document, section)
        score = float(document.get("score") or 0.0)
        link = SourceLink(
            source_label=label,
            source_kind=source_kind,
            status="resolved" if score >= 0.35 else "partial",
            score=round(score, 4),
            publisher=str(document.get("publisher") or preferred_publisher(source_kind, row.ministere) or ""),
            doc_id=str(document.get("doc_id") or ""),
            doc_short_id=str(document.get("short_id") or ""),
            doc_title=str(document.get("title") or document.get("full_title") or ""),
            warning="" if score >= 0.35 else "Low-confidence document match.",
        )
        if section:
            link.section_id = str(section.get("section_id") or "")
            link.section_heading = str(section.get("heading") or section.get("heading_path") or "")
        if chunk:
            link.chunk_table = str(chunk.get("chunk_table") or "")
            link.chunk_id = str(chunk.get("chunk_id") or "")
        return link

    def find_best_section(
        self,
        document: dict[str, Any],
        label: str,
        row: RawGoldsetRow,
    ) -> dict[str, Any] | None:
        doc_id = document.get("doc_id")
        if not doc_id:
            return None
        sql = """
            SELECT section_id, heading, heading_path, left(coalesce(section_markdown, markdown_content, ''), 2000) AS section_text
            FROM public.rag_sections
            WHERE doc_id = %s
            LIMIT 200
        """
        sections = self.fetchall(sql, (doc_id,))
        if not sections:
            return None
        needle = " ".join([label, row.question, row.gold_answer[:500], " ".join(row.keywords)])
        ranked = sorted(
            sections,
            key=lambda section: score_text_match(needle, " ".join(str(section.get(k) or "") for k in ("heading", "heading_path", "section_text"))),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def find_best_chunk(self, document: dict[str, Any], section: dict[str, Any] | None) -> dict[str, Any] | None:
        doc_id = str(document.get("doc_id") or "")
        short_id = str(document.get("short_id") or "")
        section_id = str(section.get("section_id") or "") if section else ""
        for table, id_col in (
            ("rag_chunks_matte", "hash_id"),
            ("rag_chunks_service_public", "hash_id"),
            ("rag_chunks_rgrh", "hash_id"),
        ):
            conditions = []
            params: list[Any] = []
            if section_id:
                conditions.append("section_id = %s")
                params.append(section_id)
            if doc_id:
                conditions.append("source_document_id = %s")
                params.append(doc_id)
            if short_id:
                conditions.append("short_id = %s")
                params.append(short_id)
            if not conditions:
                continue
            sql = f"""
                SELECT '{table}' AS chunk_table, {id_col} AS chunk_id, section_id, chunk_index
                FROM public.{table}
                WHERE {" OR ".join(conditions)}
                ORDER BY
                    CASE WHEN section_id = %s THEN 0 ELSE 1 END,
                    chunk_index NULLS LAST,
                    {id_col}
                LIMIT 1
            """
            try:
                rows = self.fetchall(sql, tuple(params + [section_id]))
            except Exception:
                rows = []
            if rows:
                return rows[0]

        if section_id or doc_id or short_id:
            conditions = []
            params = []
            if section_id:
                conditions.append("section_id = %s")
                params.append(section_id)
            if doc_id:
                conditions.append("doc_id = %s")
                params.append(doc_id)
            sql = f"""
                SELECT 'rag_chunks_test' AS chunk_table, chunk_id, section_id
                FROM public.rag_chunks_test
                WHERE {" OR ".join(conditions)}
                ORDER BY chunk_id
                LIMIT 1
            """
            try:
                rows = self.fetchall(sql, tuple(params))
            except Exception:
                rows = []
            if rows:
                return rows[0]
        return None

    def _connection(self) -> psycopg.Connection[Any]:
        # Reuse a single autocommit connection for the whole prepare run: a
        # goldset resolves many queries per row, and a fresh connect per query
        # would pay a TCP/TLS/auth handshake hundreds of times. Autocommit keeps
        # each read independent so one failing statement cannot poison the rest.
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        return self._conn

    def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection().execute(sql, params).fetchall()]

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


class NullResolver:
    """Resolver used for local smoke tests without DB access."""

    def close(self) -> None:
        return None

    def resolve_row(self, row: RawGoldsetRow) -> PreparedRow:
        labels = split_source_labels(row.sources)
        return PreparedRow(
            raw=row,
            source_labels=labels,
            links=[
                SourceLink(
                    source_label=label,
                    source_kind=classify_source_label(label, row.ministere),
                    status="unresolved",
                    warning="No DSN provided; source relinking skipped.",
                )
                for label in labels
            ],
        )


def prepare_rows(rows: list[RawGoldsetRow], resolver: Any) -> list[PreparedRow]:
    return [resolver.resolve_row(row) for row in rows]


def apply_extra_tags(rows: list[RawGoldsetRow], extra_tags: Sequence[str]) -> list[RawGoldsetRow]:
    normalized_tags = [tag.strip() for tag in extra_tags if tag and tag.strip()]
    if not normalized_tags:
        return rows
    return [row.with_extra_tags(normalized_tags) for row in rows]


def write_outputs(prepared_rows: list[PreparedRow], *, output_dir: Path, goldset_name: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    enriched_path = output_dir / f"{goldset_name}.enriched.csv"
    links_path = output_dir / f"{goldset_name}.source_links.csv"

    with enriched_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENRICHED_COLUMNS)
        writer.writeheader()
        for row in prepared_rows:
            writer.writerow(row.to_enriched_dict())

    with links_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_LINK_COLUMNS)
        writer.writeheader()
        for row in prepared_rows:
            for link in row.links:
                writer.writerow(
                    {
                        "goldset_name": row.raw.goldset_name,
                        "row_id": row.raw.row_id,
                        **{col: getattr(link, col) for col in SOURCE_LINK_COLUMNS if hasattr(link, col)},
                    }
                )

    return enriched_path, links_path


def upsert_prepared_rows(
    prepared_rows: list[PreparedRow],
    *,
    dsn: str,
    replace_goldset: bool = False,
) -> dict[str, Any]:
    if not prepared_rows:
        raise ValueError("No prepared rows to upsert")

    goldset_names = sorted({row.raw.goldset_name for row in prepared_rows if row.raw.goldset_name})
    insert_sql = """
        INSERT INTO public.goldset_questions_v2 (
            question,
            gold_answer,
            gold_sources,
            theme,
            source,
            goldset_name,
            comment,
            tags
        )
        VALUES (
            %(question)s,
            %(gold_answer)s,
            %(gold_sources)s,
            %(theme)s,
            %(source)s,
            %(goldset_name)s,
            %(comment)s,
            %(tags)s
        )
        ON CONFLICT (question) DO UPDATE
        SET
            gold_answer = EXCLUDED.gold_answer,
            gold_sources = EXCLUDED.gold_sources,
            theme = EXCLUDED.theme,
            source = EXCLUDED.source,
            goldset_name = EXCLUDED.goldset_name,
            comment = EXCLUDED.comment,
            tags = EXCLUDED.tags,
            updated_at = NOW()
    """
    payload_rows = [
        {
            "question": row.raw.question,
            "gold_answer": row.raw.gold_answer,
            "gold_sources": json.dumps(row.gold_sources(), ensure_ascii=False),
            "theme": row.raw.theme,
            "source": row.raw.ministere,
            "goldset_name": row.raw.goldset_name,
            "comment": json.dumps(
                {
                    "ministere": row.raw.ministere,
                    "source_labels": row.source_labels,
                    "gold_source_links": [link.compact() for link in row.links],
                    "gold_chunk_ids": row.gold_chunk_ids(),
                    "gold_section_ids": row.gold_section_ids(),
                    "link_status": row.link_status,
                    "link_warnings": _unique_preserve_order([*row.warnings, *(link.warning for link in row.links if link.warning)]),
                },
                ensure_ascii=False,
            ),
            "tags": row.raw.keywords,
        }
        for row in prepared_rows
    ]

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        ensure_goldset_table(conn)
        deleted_rows = 0
        if replace_goldset and goldset_names:
            deleted_rows = (
                conn.execute(
                    "DELETE FROM public.goldset_questions_v2 WHERE goldset_name = ANY(%s)",
                    (goldset_names,),
                ).rowcount
                or 0
            )
        with conn.cursor() as cur:
            cur.executemany(insert_sql, payload_rows)
        conn.commit()

    return {
        "loaded_rows": len(payload_rows),
        "deleted_rows": deleted_rows,
        "goldset_names": goldset_names,
    }


def ensure_goldset_table(conn: psycopg.Connection) -> None:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'goldset_questions_v2'
        ) AS table_exists
        """
    ).fetchone()
    if not row or not row["table_exists"]:
        raise RuntimeError("Required table public.goldset_questions_v2 is missing.")


def resolve_target_dsn(explicit_dsn: str | None, dsn_env: str) -> str | None:
    if explicit_dsn:
        return explicit_dsn
    return os.getenv(dsn_env, "").strip() or None


def parse_column_overrides(values: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid --column-alias {value!r}; expected canonical=actual")
        canonical, actual = value.split("=", 1)
        canonical = canonical.strip()
        actual = actual.strip()
        if canonical not in RAW_COLUMN_ALIASES:
            raise ValueError(f"Unknown canonical column {canonical!r}")
        overrides[canonical] = actual
    return overrides


def upload_goldset_outputs(
    *,
    source_dir: Path,
    goldset_name: str,
    repo_id: str,
    subdir: str,
    token: str | None,
    create_repo: bool,
) -> str:
    if not token:
        raise RuntimeError("HF_TOKEN or HUGGINGFACE_HUB_TOKEN is required for --upload-hf")
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    if create_repo:
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=True, exist_ok=True)

    path_in_repo = subdir.strip("/")
    api.upload_folder(
        folder_path=source_dir,
        path_in_repo=path_in_repo,
        repo_id=repo_id,
        repo_type="dataset",
        allow_patterns=[f"{goldset_name}.*.csv"],
        commit_message=f"Upload prepared goldset {goldset_name}",
    )
    return f"hf://datasets/{repo_id}/{path_in_repo}" if path_in_repo else f"hf://datasets/{repo_id}"


def download_goldset_outputs(
    *,
    output_dir: Path,
    goldset_name: str,
    repo_id: str,
    subdir: str,
    revision: str | None,
    token: str | None,
) -> list[Path]:
    from huggingface_hub import hf_hub_download, list_repo_files

    files = list_repo_files(repo_id=repo_id, repo_type="dataset", revision=revision, token=token)
    prefix = f"{subdir.strip('/')}/" if subdir else ""
    wanted = [
        path for path in files if path.startswith(prefix) and Path(path).name in {f"{goldset_name}.enriched.csv", f"{goldset_name}.source_links.csv"}
    ]
    if not wanted:
        raise RuntimeError(f"No prepared goldset files found for {goldset_name!r} in {repo_id}/{subdir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for filename in wanted:
        path = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=filename,
            revision=revision,
            token=token,
            local_dir=output_dir,
        )
        downloaded.append(Path(path))
    return downloaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare a private goldset CSV and relink sources to corpus IDs.")
    parser.add_argument("--input", type=Path, help="Raw or enriched CSV input path.")
    parser.add_argument("--goldset-name", default=os.getenv("ASSISTANT_RH_PRIVATE_GOLDSET_NAME", DEFAULT_PRIVATE_GOLDSET_NAME))
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--target-dsn", default=None)
    parser.add_argument("--target-dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--column-alias", action="append", default=[], help="Override raw column mapping, e.g. question=Question")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--allow-unresolved", action="store_true", help="Return success even when source links are unresolved.")
    parser.add_argument("--extra-tag", action="append", default=[], help="Append a tag to every prepared DB/CSV row, e.g. iteration2.")
    parser.add_argument("--upsert-db", action="store_true", help="Upsert prepared rows into public.goldset_questions_v2.")
    parser.add_argument("--replace-goldset", action="store_true", help="Delete existing rows for this goldset name before --upsert-db.")
    parser.add_argument("--skip-db", action="store_true", help="Prepare rows without DB relinking; useful for local smoke tests.")
    parser.add_argument("--upload-hf", action="store_true")
    parser.add_argument("--download-hf", action="store_true")
    parser.add_argument(
        "--hf-repo-id",
        default=(os.getenv("ASSISTANT_RH_PRIVATE_GOLDSET_REPO") or os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REPO") or DEFAULT_PRIVATE_GOLDSET_REPO),
    )
    parser.add_argument("--hf-subdir", default=os.getenv("ASSISTANT_RH_PRIVATE_GOLDSET_SUBDIR"))
    parser.add_argument("--hf-revision", default=os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REVISION") or None)
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN"))
    parser.add_argument("--create-hf-repo", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    args = build_parser().parse_args(argv)
    goldset_name = args.goldset_name.strip()
    if not goldset_name:
        raise SystemExit("--goldset-name cannot be empty")

    default_subdir = DEFAULT_PRIVATE_GOLDSET_SUBDIR_TEMPLATE.format(goldset_name=goldset_name)
    hf_subdir = args.hf_subdir or default_subdir
    output_dir = args.output_dir or DEFAULT_CACHE_DIR / goldset_name

    if args.download_hf:
        downloaded = download_goldset_outputs(
            output_dir=output_dir,
            goldset_name=goldset_name,
            repo_id=args.hf_repo_id,
            subdir=hf_subdir,
            revision=args.hf_revision,
            token=args.hf_token,
        )
        print(json.dumps({"status": "ok", "downloaded": [str(path) for path in downloaded]}, ensure_ascii=False, indent=2))
        return 0

    if not args.input:
        raise SystemExit("--input is required unless --download-hf is used")

    rows = read_csv_rows(args.input)
    if args.validate_only:
        if rows and all(col in rows[0] for col in ENRICHED_COLUMNS):
            errors = validate_enriched_rows(rows)
        else:
            raw_rows = parse_raw_rows(rows, goldset_name=goldset_name, column_overrides=parse_column_overrides(args.column_alias))
            raw_rows = apply_extra_tags(raw_rows, args.extra_tag)
            errors = validate_raw_rows(raw_rows)
        if errors:
            for error in errors:
                print(error)
            return 1
        print(json.dumps({"status": "ok", "input": str(args.input)}, ensure_ascii=False, indent=2))
        return 0

    raw_rows = parse_raw_rows(rows, goldset_name=goldset_name, column_overrides=parse_column_overrides(args.column_alias))
    raw_rows = apply_extra_tags(raw_rows, args.extra_tag)
    errors = validate_raw_rows(raw_rows)
    if errors:
        raise SystemExit("\n".join(errors))

    dsn = None if args.skip_db else resolve_target_dsn(args.target_dsn, args.target_dsn_env)
    resolver = GoldsetResolver(dsn) if dsn else NullResolver()
    try:
        prepared = prepare_rows(raw_rows, resolver)
    finally:
        resolver.close()
    enriched_path, links_path = write_outputs(prepared, output_dir=output_dir, goldset_name=goldset_name)

    status_counts: dict[str, int] = {}
    for row in prepared:
        status_counts[row.link_status] = status_counts.get(row.link_status, 0) + 1

    hf_path = None
    if args.upload_hf:
        hf_path = upload_goldset_outputs(
            source_dir=output_dir,
            goldset_name=goldset_name,
            repo_id=args.hf_repo_id,
            subdir=hf_subdir,
            token=args.hf_token,
            create_repo=args.create_hf_repo,
        )

    db_result = None
    if args.upsert_db:
        if not dsn:
            raise SystemExit("--upsert-db requires a DSN; remove --skip-db or pass --target-dsn/--target-dsn-env")
        db_result = upsert_prepared_rows(prepared, dsn=dsn, replace_goldset=args.replace_goldset)

    result = {
        "status": "ok",
        "goldset_name": goldset_name,
        "row_count": len(prepared),
        "status_counts": status_counts,
        "enriched_csv": str(enriched_path),
        "source_links_csv": str(links_path),
        "hf_path": hf_path,
        "db_result": db_result,
        "target_dsn_env": args.target_dsn_env if dsn else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not args.allow_unresolved and any(row.link_status == "unresolved" for row in prepared):
        return 2
    return 0


def preferred_publisher(source_kind: str, ministere: str = "") -> str | None:
    if source_kind == "service_public":
        return "Service-Public"
    if source_kind == "matte":
        return "MATTE"
    if source_kind == "mso":
        return "MSO"
    if source_kind == "rgrh":
        return "RGRH"
    ministry = normalize_text(ministere)
    if ministry == "matte":
        return "MATTE"
    if ministry == "mso":
        return "MSO"
    return None


def _extract_legal_reference(label: str) -> str:
    match = re.search(r"\b(?:article\s+)?([LRD]\.?\s*\d+(?:[-‑]\d+)*(?:\s*[-‑]\s*\d+)?)\b", label, re.IGNORECASE)
    if match:
        return re.sub(r"\s+", "", match.group(1)).replace("‑", "-")
    return label.strip()


def _cell(row: Mapping[str, Any], column: str) -> str:
    value = row.get(column)
    return str(value or "").strip()


def _unique_preserve_order(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out
