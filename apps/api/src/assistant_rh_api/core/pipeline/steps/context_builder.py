"""C4 context budget, whole documents, triangulation and legal references."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Dict, List, Set, cast

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.core.models.context import AggregatedSection, ContextBuildDiagnostics, ContextBuildResult, ContextItem, estimate_tokens
from assistant_rh_api.core.models.rag_configuration import ContextBuildConfig
from assistant_rh_api.core.ports.retrieval import ContentStorePort

_REF_HINT_KEYS = ("source", "title", "titre", "full_title", "text", "texte", "label", "complement")
_REF_STOPWORDS = frozenset("a art article articles au aux dans de des du en et l la le les n no par pour sur un une".split())


def _ref_tokens(value: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    tokens = {tok for tok in re.findall(r"[a-z0-9]+", ascii_text) if len(tok) > 1 and tok not in _REF_STOPWORDS}
    if "cgfp" in tokens:
        tokens.update(("code", "general", "fonction", "publique"))
    return tokens


def _match_reference_candidate(ref_entry: Mapping[str, Any], candidates: Sequence[dict[str, str]]) -> dict[str, str] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    hint_parts = [str(ref_entry[key]) for key in _REF_HINT_KEYS if isinstance(ref_entry.get(key), str) and ref_entry[key].strip()]
    if not hint_parts:
        return None
    hint_tokens = _ref_tokens(" ".join(hint_parts))
    if not hint_tokens:
        return None
    scored: list[tuple[int, float, int]] = []
    for idx, candidate in enumerate(candidates):
        cand_tokens = _ref_tokens(candidate.get("title", ""))
        if not cand_tokens:
            continue
        common = hint_tokens & cand_tokens
        if not common:
            continue
        has_specific_token = len(common) >= 2 or any(any(ch.isdigit() for ch in tok) or len(tok) >= 5 for tok in common)
        if not has_specific_token:
            continue
        scored.append((len(common), len(common) / len(cand_tokens), idx))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if len(scored) > 1 and (scored[0][0], scored[0][1]) == (scored[1][0], scored[1][1]):
        return None
    return candidates[scored[0][2]]


def thaw(value: Any) -> Any:
    """Render immutable JSON with the historical dict/list representation."""
    if isinstance(value, Mapping):
        return {key: thaw(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(child) for child in value]
    return value


class ContextBuilder:
    """
    Build the final context for the LLM generator from ranked sections.

    Strategy:
      1. If the top document is small (< ``doc_entire_threshold`` tokens),
         include **all its sections** up to ``max_full_docs``.
      2. Fill remaining budget with the highest-scored individual sections.
      3. **Triangulation**: always inject at least ``triangulation_sections``
         sections from publishers *other* than the top one.
      4. Append legal references cited inside the selected sections.
    """

    def __init__(self, config: ContextBuildConfig, content_store: ContentStorePort):
        self.config = config
        self.content_store = content_store

    async def build(self, sections: Sequence[AggregatedSection]) -> ContextBuildResult:
        sections = tuple(sections)
        if not sections:
            return ContextBuildResult((), {}, ContextBuildDiagnostics(token_budget=self.config.get_token_budget()))
        store_errors: list[str] = []

        budget = self.config.get_token_budget()
        max_full_docs = self.config.get_max_full_docs()
        doc_threshold = self.config.get_doc_entire_threshold()
        max_sections = self.config.get_max_sections()
        refs_budget = self.config.get_legal_refs_budget()

        selected: List[ContextItem] = []
        used_ids: Set[str] = set()
        tokens_used = 0
        full_doc_count = 0

        # Group by document (only sections that have a document_id). Sections
        # without a document_id are picked up by the Step-2 loop directly.
        by_doc: Dict[str, List[AggregatedSection]] = defaultdict(list)
        for s in sections:
            if s.document_id:
                by_doc[str(s.document_id)].append(s)

        # Sort doc groups by best section score (descending); compute the max
        # once and reuse it (used both for sort and for the inner gate).
        doc_max_score: Dict[str, float] = {doc_id: max((s.score or 0.0) for s in secs) for doc_id, secs in by_doc.items()}
        sorted_docs = sorted(by_doc.items(), key=lambda kv: doc_max_score[kv[0]], reverse=True)

        def _section_key(s: AggregatedSection) -> str:
            # Unique-and-stable across both loops without colliding when two
            # positional standalone chunks of a long legal article share a heading.
            if s.section_id:
                return f"sec:{s.section_id}"
            if s.chunks:
                return f"chunk:{s.publisher or ''}:{s.chunks[0].chunk_id}"
            return f"_obj_{id(s)}"

        # Step 1 – doc-entire for small documents. We always include qualifying
        # small docs (within budget and the max_full_docs cap); the original
        # legifrance-q2 ordering bug is fixed by the score-sort at the end of
        # Step 2 below, not by suppressing doc-entires here.
        for doc_id, doc_sections in sorted_docs:
            if full_doc_count >= max_full_docs:
                break
            doc_token_count = cast(int, doc_sections[0].metadata.get("doc_token_count", 0) or 0)
            if doc_token_count <= 0 or doc_token_count > doc_threshold:
                continue
            if tokens_used + doc_token_count > budget:
                continue

            doc_row = await self._load_full_document(doc_id, store_errors)
            if not doc_row or not doc_row.get("doc_markdown"):
                continue

            item = self._full_doc_to_item(doc_row, doc_sections)
            item = replace(item, metadata={**item.metadata, "is_doc_entire": True})
            # ContextItem.score on a doc-entire reflects the best section score
            # of its constituents, so the post-Step-2 sort places it correctly.
            item = replace(item, score=doc_max_score[doc_id])
            selected.append(item)
            for s in doc_sections:
                used_ids.add(_section_key(s))
            tokens_used += item.token_estimate
            full_doc_count += 1

        # Step 2 – fill with top individual sections (from selector/reranker order)
        for s in sections:
            key = _section_key(s)
            if key in used_ids:
                continue
            item = self._section_to_item(s)
            if tokens_used + item.token_estimate > budget:
                continue
            if len(selected) >= max_sections:
                break
            selected.append(item)
            used_ids.add(key)
            tokens_used += item.token_estimate

        # The sort key puts highest-scoring items first; on exact score ties,
        # standalone chunks beat doc-entires (promotion intent from review
        # finding #7) and Python's stable sort preserves retrieval order for
        # the remaining ties.
        def _sort_key(item):
            return (-(item.score or 0.0), 1 if item.metadata.get("is_doc_entire") else 0)

        # Determine the "primary publisher" for triangulation BEFORE appending —
        # use the highest-scored section in the input rather than `selected[0]`
        # so triangulation depends on the data, not on insertion ordering.
        primary_publisher = None
        if sections:
            top_section = max(sections, key=lambda s: s.score or 0.0)
            primary_publisher = top_section.publisher

        # Step 3 – triangulation (ignores budget to guarantee publisher diversity,
        # gated by triangulation_min_score so near-zero reranked noise is skipped)
        tri_added = 0
        for s in sections:
            if tri_added >= self.config.triangulation_sections:
                break
            if s.publisher == primary_publisher:
                continue
            if (s.score or 0.0) < self.config.triangulation_min_score:
                continue
            key = _section_key(s)
            if key in used_ids:
                continue
            item = self._section_to_item(s)
            item = replace(item, metadata={**item.metadata, "is_triangulation": True})
            selected.append(item)
            used_ids.add(key)
            tokens_used += item.token_estimate
            tri_added += 1

        # Single sort at the very end so triangulation items participate in the
        # final ordering — the LLM always sees the strongest evidence first,
        # regardless of which step contributed each item.
        selected.sort(key=_sort_key)

        # Step 4 – resolve legal references from rag_chunks_dgafp and inject
        refs_tokens = 0
        all_ref_numbers = self._collect_ref_numbers(selected)
        candidates_by_number = await self._resolve_reference_candidates(all_ref_numbers, store_errors) if all_ref_numbers else {}
        cid_map: dict[str, dict[str, str]] = {
            number: dict(candidates[0]) for number, candidates in candidates_by_number.items() if len(candidates) == 1
        }

        # Allocate the legal-refs budget primary-content-first: triangulation
        # items are publisher-diversity fillers and must not consume refs_budget
        # ahead of the answer-bearing primary sources, even when they outscore
        # them. Stable sort preserves the score order within each group, so the
        # prompt order (``selected``) is untouched — only the refs pass reorders.
        refs_order = sorted(range(len(selected)), key=lambda index: bool(selected[index].metadata.get("is_triangulation")))
        for index in refs_order:
            item = selected[index]
            if item.references_juridiques and refs_tokens < refs_budget:
                item, matched_refs = self._enrich_refs_with_cid(item, candidates_by_number)
                for number, resolved in matched_refs:
                    if number not in cid_map:
                        cid_map[number] = dict(resolved)
                    elif cid_map[number]["cid"] != resolved["cid"]:
                        cid_map[f"{number}@{resolved['cid']}"] = dict(resolved)
                selected[index] = item
                ref_text = self._format_references(item.references_juridiques)
                ref_tokens = estimate_tokens(ref_text)
                if refs_tokens + ref_tokens <= refs_budget:
                    selected[index] = replace(
                        item,
                        content=item.content + f"\n\n---\nReferences juridiques :\n{ref_text}",
                        token_estimate=item.token_estimate + ref_tokens,
                    )
                    tokens_used += ref_tokens
                    refs_tokens += ref_tokens

        return ContextBuildResult(
            tuple(selected),
            cid_map,
            ContextBuildDiagnostics(tokens_used, budget, full_doc_count, tri_added, refs_tokens, tuple(store_errors)),
        )

    async def _load_full_document(self, doc_id: str, errors: list[str]) -> dict | None:
        try:
            documents = await self.content_store.documents((doc_id,))
        except (DatabaseUnavailable, DatabaseFailure, DatabaseConflict) as exc:
            errors.append(exc.code)
            return None
        doc = next((doc for doc in documents if doc.doc_id == doc_id), None)
        if doc is None:
            return None
        return {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "source_url": doc.url,
            "publisher": doc.publisher,
            "doc_markdown": doc.markdown,
            "token_count": doc.token_count,
        }

    def _full_doc_to_item(self, doc_row: dict, matched_sections: List[AggregatedSection]) -> ContextItem:
        """Convert a rag_documents row into a single ContextItem with the full document content."""
        best_score = max((s.score for s in matched_sections), default=0.0)
        merged_refs = self._merge_section_references(matched_sections)
        return ContextItem(
            section_id=None,
            heading=doc_row.get("title", ""),
            content=doc_row.get("doc_markdown", ""),
            score=best_score,
            publisher=doc_row.get("publisher", ""),
            document_title=doc_row.get("title", ""),
            document_url=doc_row.get("source_url"),
            references_juridiques=merged_refs,
            token_estimate=doc_row.get("token_count") or estimate_tokens(doc_row.get("doc_markdown", "")),
            metadata={
                "doc_id": str(doc_row.get("doc_id", "")),
                "doc_title": doc_row.get("title", ""),
                "doc_url": doc_row.get("source_url"),
                "doc_publisher": doc_row.get("publisher", ""),
                "doc_token_count": doc_row.get("token_count", 0),
            },
        )

    @staticmethod
    def _merge_section_references(matched_sections: Sequence[AggregatedSection]) -> Any:
        raw_blocks = [thaw(s.references_juridiques) for s in matched_sections if s.references_juridiques]
        if not raw_blocks:
            return None
        if len(raw_blocks) == 1:
            return raw_blocks[0]
        merged: list[Any] = []
        seen: set[str] = set()
        all_list_like = True
        for block in raw_blocks:
            parsed = block
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except (ValueError, json.JSONDecodeError):
                    all_list_like = False
                    break
            if not isinstance(parsed, list):
                all_list_like = False
                break
            for entry in parsed:
                canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                if canonical not in seen:
                    seen.add(canonical)
                    merged.append(entry)
        if all_list_like:
            return merged or None
        return raw_blocks[0]

    def _section_to_item(self, s: AggregatedSection) -> ContextItem:
        meta = dict(s.metadata)
        if s.heading_path:
            meta["heading_path"] = s.heading_path
        return ContextItem(
            section_id=s.section_id,
            heading=s.heading,
            content=s.markdown,
            score=s.score,
            publisher=s.publisher,
            document_title=cast(str | None, s.metadata.get("doc_title", "")),
            document_url=cast(str | None, s.metadata.get("doc_url")),
            references_juridiques=s.references_juridiques,
            token_estimate=s.token_estimate,
            metadata=meta,
        )

    @staticmethod
    def _collect_ref_numbers(items: List[ContextItem]) -> List[str]:
        """Extract all article numbers from references_juridiques across items."""
        numbers = []
        for item in items:
            refs = thaw(item.references_juridiques)
            if not refs:
                continue
            if isinstance(refs, str):
                try:
                    refs = json.loads(refs)
                except (ValueError, json.JSONDecodeError):
                    continue
            if isinstance(refs, list):
                for r in refs:
                    if isinstance(r, dict) and r.get("number"):
                        numbers.append(r["number"])
        return list(dict.fromkeys(numbers))

    async def _resolve_reference_candidates(self, numbers: List[str], errors: list[str]) -> dict[str, list[dict[str, str]]]:
        try:
            references = await self.content_store.references(tuple(numbers))
        except (DatabaseUnavailable, DatabaseFailure, DatabaseConflict) as exc:
            errors.append(exc.code)
            return {}
        grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
        for ref in references:
            if ref.cid:
                grouped[ref.number][ref.cid] = {"cid": ref.cid, "url": ref.url, "title": ref.title}
        return {number: list(by_cid.values()) for number, by_cid in grouped.items() if by_cid}

    @staticmethod
    def _enrich_refs_with_cid(
        item: ContextItem,
        candidates_by_number: Mapping[str, Sequence[dict[str, str]]],
    ) -> tuple[ContextItem, list[tuple[str, dict[str, str]]]]:
        """Add cid/url to references_juridiques entries when unambiguous or title-disambiguated."""
        refs = thaw(item.references_juridiques)
        if not refs:
            return item, []
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except (ValueError, json.JSONDecodeError):
                return item, []
        matched: list[tuple[str, dict[str, str]]] = []
        if isinstance(refs, list):
            for r in refs:
                if isinstance(r, dict) and r.get("number") in candidates_by_number:
                    number = str(r["number"])
                    resolved = _match_reference_candidate(r, candidates_by_number[number])
                    if resolved is not None:
                        r.update(resolved)
                        matched.append((number, resolved))
            return replace(item, references_juridiques=tuple(refs)), matched
        return item, []

    @staticmethod
    def _format_references(refs) -> str:
        refs = thaw(refs)
        if isinstance(refs, str):
            return refs
        if isinstance(refs, dict):
            lines = []
            for key, val in refs.items():
                lines.append(f"- {key}: {val}")
            return "\n".join(lines)
        if isinstance(refs, list):
            return "\n".join(f"- {r}" for r in refs)
        return str(refs)
