"""Matérialisation follow-live des articles Légifrance via PISTE.

La TOC ``lawDecree``/``tableMatieres`` signale la version courante, mais ne
constitue pas un artefact de contenu. Ce module transforme la réponse officielle
``consult/getArticle`` en un bundle bronze/silver/gold et réconcilie les anciens
identifiants JORFARTI avec le CID chronique LEGIARTI retourné par l'API.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence

from .piste import CodeArticle
from .xml_article_parser import normalize_legifrance_category


class _ArticleHtmlParser(HTMLParser):
    """Extraction texte légère qui conserve les séparations juridiques utiles."""

    _BLOCK_TAGS = frozenset({"br", "div", "li", "p", "table", "tbody", "td", "th", "tr", "ul", "ol"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_legal_text(value: Any) -> str:
    """HTML PISTE → texte lisible, avec paragraphes et listes préservés."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parser = _ArticleHtmlParser()
    parser.feed(raw)
    parser.close()
    lines = [re.sub(r"[ \t\f\v]+", " ", html.unescape(line)).strip() for line in "".join(parser.parts).splitlines()]
    compact: list[str] = []
    for line in lines:
        if line:
            compact.append(line)
        elif compact and compact[-1] != "":
            compact.append("")
    return "\n\n".join(" ".join(block.split()) for block in "\n".join(compact).split("\n\n") if block.strip())


def _date_iso(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"-?\d+", raw):
        timestamp = int(raw)
        if abs(timestamp) > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    match = re.match(r"\d{4}-\d{2}-\d{2}", raw)
    return match.group(0) if match else raw


def _article_payload(response: Mapping[str, Any]) -> Mapping[str, Any]:
    article = response.get("article") or response
    if not isinstance(article, Mapping):
        raise RuntimeError("Réponse PISTE getArticle sans objet 'article'.")
    return article


def _preferred_text_title(article: Mapping[str, Any]) -> Mapping[str, Any]:
    titles = [title for title in article.get("textTitles") or [] if isinstance(title, Mapping)]
    if not titles:
        return {}

    def rank(title: Mapping[str, Any]) -> tuple[int, str, str]:
        identifiers = [str(title.get(key) or "").upper() for key in ("cid", "id")]
        consolidated = int(any(identifier.startswith("LEGITEXT") for identifier in identifiers))
        return consolidated, str(title.get("dateDebut") or ""), str(title.get("id") or "")

    return max(titles, key=rank)


def _canonical_link(link: Mapping[str, Any]) -> dict[str, Any]:
    """Copie le contrat de liens PISTE sous ses noms canoniques gold."""
    return {
        "textCid": link.get("textCid") or link.get("cidtexte"),
        "linkType": link.get("linkType") or link.get("typelien"),
        "numTexte": link.get("numTexte") or link.get("numtexte"),
        "articleId": link.get("articleId") or link.get("id"),
        "dateDebut": _date_iso(link.get("dateDebut")),
        "datePubli": _date_iso(link.get("datePubli") or link.get("datePubliTexte")),
        "parentCid": link.get("parentCid"),
        "textTitle": link.get("textTitle") or link.get("label"),
        "articleNum": link.get("articleNum") or link.get("num"),
        "natureText": link.get("natureText") or link.get("naturetexte"),
        "linkOrientation": link.get("linkOrientation") or link.get("sens"),
    }


def canonical_article_from_response(expected: CodeArticle, response: Mapping[str, Any]) -> CodeArticle:
    """Remplace l'identité TOC JORFARTI par le CID chronique de getArticle."""
    article = _article_payload(response)
    version_id = str(article.get("id") or expected.version_id or expected.cid).strip().upper()
    cid = str(article.get("cid") or expected.cid).strip().upper()
    if not version_id.startswith("LEGIARTI"):
        raise RuntimeError(f"getArticle({expected.version_id or expected.cid}) sans version_id LEGIARTI exploitable.")
    if not cid.startswith("LEGIARTI"):
        raise RuntimeError(f"getArticle({version_id}) sans CID chronique LEGIARTI (reçu: {cid or 'vide'}).")
    aliases = {
        str(alias).strip().upper()
        for alias in (*expected.alias_ids, expected.cid, expected.version_id, version_id, cid)
        if str(alias or "").strip()
    }
    return CodeArticle(
        cid=cid,
        etat=str(article.get("etat") or expected.etat),
        num=str(article.get("num") or expected.num or "").strip() or None,
        version_id=version_id,
        alias_ids=tuple(sorted(aliases)),
    )


def bronze_payload_from_response(expected: CodeArticle, response: Mapping[str, Any]) -> tuple[CodeArticle, dict[str, Any]]:
    """Projette une réponse getArticle officielle vers le contrat bronze."""
    article = _article_payload(response)
    canonical = canonical_article_from_response(expected, response)
    title = _preferred_text_title(article)
    body = str(article.get("texte") or "").strip() or html_to_legal_text(article.get("texteHtml"))
    if not body:
        raise RuntimeError(f"getArticle({canonical.version_id}) sans contenu textuel exploitable.")

    full_title = str(title.get("titreLong") or title.get("titre") or canonical.version_id).strip()
    short_title = str(title.get("titre") or full_title).strip()
    category = normalize_legifrance_category(str(title.get("nature") or "")) or "DECRET"
    full_sections_title = str(article.get("fullSectionsTitre") or "").strip() or None
    nota = str(article.get("nota") or "").strip() or html_to_legal_text(article.get("notaHtml")) or None

    def links(key: str) -> list[dict[str, Any]]:
        return [_canonical_link(item) for item in article.get(key) or [] if isinstance(item, Mapping)]

    payload = {
        "asset_type": "article",
        "article_id": canonical.version_id,
        "version_id": canonical.version_id,
        "cid": canonical.cid,
        "num_article": canonical.num,
        "title": short_title,
        "full_title": full_title,
        "subtitles": full_sections_title,
        "full_sections_title": full_sections_title,
        "text": body,
        "category": category,
        "status": canonical.etat,
        "source_name": "PISTE",
        "section_parent_cid": article.get("sectionParentCid") or article.get("sectionParentId"),
        "section_parent_titre": article.get("sectionParentTitre"),
        "code_id": (title.get("cid") or title.get("id")) if category == "CODE" else None,
        "state": canonical.etat.lower(),
        "date_version": _date_iso(article.get("dateDebut")),
        "start_date": _date_iso(article.get("dateDebut")),
        "end_date": _date_iso(article.get("dateFin")),
        "ministry": None,
        "nota": nota,
        "lien_citations": links("lienCitations"),
        "lien_modifications": links("lienModifications"),
        "lien_concordes": links("lienConcordes"),
        "comporte_liens_sp": bool(article.get("comporteLiensSP")),
        "origin": "piste_get_article",
        "search_source": "piste_follow_live",
    }
    return canonical, payload


def silver_version_index(documents: Iterable[Mapping[str, Any]]) -> tuple[set[str], dict[str, str]]:
    """Versions matérialisées + index version→CID chronique depuis silver."""
    version_ids: set[str] = set()
    version_to_cid: dict[str, str] = {}
    for document in documents:
        metadata = document.get("metadata") or {}
        short_id = str(document.get("short_id") or "").strip().upper()
        canonical = str(metadata.get("cid") or short_id).strip().upper()
        explicit_versions = {
            str(value).strip().upper()
            for value in (metadata.get("article_id"), metadata.get("version_id"))
            if str(value or "").strip()
        }
        aliases = {*explicit_versions, short_id}
        if explicit_versions:
            version_ids.update(explicit_versions)
        else:
            # Pour un artefact historique sans metadata, short_id est la seule
            # version démontrée présente. Toute autre version TOC devra donc
            # être matérialisée (fail-safe plutôt que contenu potentiellement
            # figé et considéré à tort comme à jour).
            version_ids.add(short_id)
        if canonical.startswith("LEGIARTI"):
            for alias in aliases:
                version_to_cid[alias] = canonical
    return version_ids, version_to_cid


def canonicalize_toc_from_silver(
    toc_by_text: Mapping[str, Sequence[CodeArticle]],
    documents: Iterable[Mapping[str, Any]],
) -> dict[str, list[CodeArticle]]:
    """Réutilise les mappings version→chronique déjà matérialisés en silver."""
    _, version_to_cid = silver_version_index(documents)
    output: dict[str, list[CodeArticle]] = {}
    for text_uid, articles in toc_by_text.items():
        normalized: list[CodeArticle] = []
        for article in articles:
            version_id = str(article.version_id or article.cid).strip().upper()
            canonical = version_to_cid.get(version_id)
            if canonical and canonical != article.cid:
                aliases = tuple(sorted({*article.alias_ids, article.cid, version_id, canonical}))
                article = CodeArticle(canonical, article.etat, article.num, version_id, aliases)
            normalized.append(article)
        output[text_uid] = normalized
    return output


def replace_toc_article(
    toc_by_text: Mapping[str, Sequence[CodeArticle]],
    replacement: CodeArticle,
) -> dict[str, list[CodeArticle]]:
    """Remplace partout la version concernée après résolution getArticle."""
    output: dict[str, list[CodeArticle]] = {}
    replacement_aliases = set(replacement.alias_ids) | {replacement.version_id, replacement.cid}
    for text_uid, articles in toc_by_text.items():
        output[text_uid] = [
            replacement
            if ({article.cid, article.version_id, *article.alias_ids} & replacement_aliases)
            and str(article.version_id or article.cid).upper() == replacement.version_id
            else article
            for article in articles
        ]
    return output


@dataclass(frozen=True)
class LiveArtifactBundle:
    article: CodeArticle
    document: dict[str, Any]
    sections: list[dict[str, Any]]
    chunks: list[dict[str, Any]]


class LegifranceLiveMaterializer:
    """Persiste une réponse PISTE brute puis ses projections silver/gold."""

    def __init__(self, pipeline: Any, *, object_storage: Any | None = None, target_env: str = "staging") -> None:
        self.pipeline = pipeline
        self.object_storage = object_storage
        self.target_env = target_env
        self.materialized: list[str] = []

    def materialize(self, expected: CodeArticle, response: Mapping[str, Any]) -> LiveArtifactBundle:
        canonical, payload = bronze_payload_from_response(expected, response)
        self.pipeline.bronze_repo.save_piste_article_payload(canonical.version_id, dict(response))
        asset = self.pipeline.bronze_builder.persist_article_payload(self.pipeline.bronze_repo, payload)
        silver_bundle = self.pipeline.run_silver([asset])[0]
        gold_bundle = self.pipeline.run_gold([silver_bundle])[0]
        self.materialized.append(canonical.version_id)
        return LiveArtifactBundle(canonical, silver_bundle.document, list(silver_bundle.sections), list(gold_bundle.chunks))

    def sync(self) -> dict[str, str] | None:
        if self.object_storage is None or not self.materialized:
            return None
        return self.object_storage.sync_medallion_root(
            self.pipeline.config.paths.root_dir,
            self.target_env,
            source_name="legifrance",
            delete=False,
            include_layers=("bronze", "silver", "gold"),
        )


__all__ = [
    "LegifranceLiveMaterializer",
    "LiveArtifactBundle",
    "bronze_payload_from_response",
    "canonical_article_from_response",
    "canonicalize_toc_from_silver",
    "html_to_legal_text",
    "replace_toc_article",
    "silver_version_index",
]
