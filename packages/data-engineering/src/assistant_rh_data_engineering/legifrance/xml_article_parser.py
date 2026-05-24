from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .helpers import build_legifrance_article_url, clean_nullable


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _flatten_xml_content(node: ET.Element | None) -> str:
    if node is None:
        return ""
    blocks: list[str] = []
    for child in list(node):
        text = _normalize_text("".join(child.itertext()))
        if text:
            blocks.append(text)
    if blocks:
        return "\n\n".join(blocks)
    return _normalize_text("".join(node.itertext()))


def _findtext(element: ET.Element, path: str) -> str | None:
    node = element.find(path)
    if node is None or node.text is None:
        return None
    return clean_nullable(_normalize_text(node.text))


def normalize_legifrance_category(value: str | None) -> str | None:
    raw = str(clean_nullable(value) or "").upper()
    if not raw:
        return None
    if "ARRETE" in raw:
        return "ARRETE"
    if "DECRET" in raw:
        return "DECRET"
    if "CODE" in raw:
        return "CODE"
    if "LOI" in raw:
        return "LOI"
    return raw


def _select_best_title_node(
    title_nodes: list[ET.Element],
    article_start_date: str | None,
) -> ET.Element | None:
    preferred_node = None
    preferred_rank: tuple[int, int, str] | None = None

    for index, title_node in enumerate(title_nodes):
        title_text = _normalize_text("".join(title_node.itertext()))
        if not title_text:
            continue

        title_id = str(title_node.attrib.get("id_txt") or "")
        title_start = clean_nullable(title_node.attrib.get("debut"))
        title_end = clean_nullable(title_node.attrib.get("fin"))
        is_legitext = int(title_id.startswith("LEGITEXT"))
        matches_article_date = int(
            bool(article_start_date)
            and (title_start or "") <= article_start_date <= (title_end or "9999-12-31")
        )
        rank = (matches_article_date, is_legitext, f"{title_start or ''}|{index}")
        if preferred_rank is None or rank > preferred_rank:
            preferred_node = title_node
            preferred_rank = rank

    return preferred_node


def _parse_links(root: ET.Element) -> dict[str, list[dict[str, Any]]]:
    result = {
        "lien_citations": [],
        "lien_modifications": [],
        "lien_concordes": [],
    }
    for lien in root.findall("./LIENS/LIEN"):
        label = _normalize_text("".join(lien.itertext()))
        item = {
            "id": clean_nullable(lien.attrib.get("id")),
            "cidtexte": clean_nullable(lien.attrib.get("cidtexte")),
            "naturetexte": clean_nullable(lien.attrib.get("naturetexte")),
            "nortexte": clean_nullable(lien.attrib.get("nortexte")),
            "num": clean_nullable(lien.attrib.get("num")),
            "numtexte": clean_nullable(lien.attrib.get("numtexte")),
            "sens": clean_nullable(lien.attrib.get("sens")),
            "typelien": clean_nullable(lien.attrib.get("typelien")),
            "label": label or None,
        }
        typelien = str(item["typelien"] or "").upper()
        if "CONCORD" in typelien:
            result["lien_concordes"].append(item)
        elif "MODIF" in typelien:
            result["lien_modifications"].append(item)
        else:
            result["lien_citations"].append(item)
    return result


def parse_article_xml(article_path: Path) -> dict[str, Any]:
    root = ET.parse(article_path).getroot()
    article_id = _findtext(root, "./META/META_COMMUN/ID") or article_path.stem
    num_article = _findtext(root, "./META/META_SPEC/META_ARTICLE/NUM") or article_id
    status = _findtext(root, "./META/META_SPEC/META_ARTICLE/ETAT") or "VIGUEUR"
    start_date = _findtext(root, "./META/META_SPEC/META_ARTICLE/DATE_DEBUT")
    end_date = _findtext(root, "./META/META_SPEC/META_ARTICLE/DATE_FIN")
    nota = _flatten_xml_content(root.find("./NOTA/CONTENU")) or None
    body = _flatten_xml_content(root.find("./BLOC_TEXTUEL/CONTENU"))

    texte_context = root.find("./CONTEXTE/TEXTE")
    category = normalize_legifrance_category(
        texte_context.attrib.get("nature") if texte_context is not None else None
    )
    titre_nodes = texte_context.findall("./TITRE_TXT") if texte_context is not None else []
    selected_title_node = _select_best_title_node(titre_nodes, start_date)
    full_title = (
        _normalize_text("".join(selected_title_node.itertext()))
        if selected_title_node is not None
        else article_id
    )
    short_title = clean_nullable(
        selected_title_node.attrib.get("c_titre_court") if selected_title_node is not None else None
    ) or full_title

    tm_nodes = texte_context.findall(".//TITRE_TM") if texte_context is not None else []
    subtitles_parts: list[str] = []
    section_parent_cid = None
    section_parent_titre = None
    for tm_node in tm_nodes:
        titre = _normalize_text("".join(tm_node.itertext()))
        if not titre:
            continue
        subtitles_parts.append(titre)
        section_parent_cid = clean_nullable(tm_node.attrib.get("id"))
        section_parent_titre = titre

    subtitles = " > ".join(subtitles_parts) if subtitles_parts else None
    links = _parse_links(root)

    return {
        "article_id": article_id,
        "cid": article_id,
        "num_article": num_article,
        "title": short_title,
        "full_title": full_title,
        "subtitles": subtitles,
        "full_sections_title": subtitles,
        "text": body,
        "source_url": build_legifrance_article_url(article_id, category),
        "category": category,
        "status": status,
        "source_name": None,
        "section_parent_cid": section_parent_cid,
        "section_parent_titre": section_parent_titre,
        "code_id": None,
        "state": status.lower(),
        "date_version": None,
        "start_date": start_date,
        "end_date": end_date,
        "ministry": clean_nullable(
            texte_context.attrib.get("ministere") if texte_context is not None else None
        ),
        "nota": nota,
        "origin": "legi_bulk",
        "search_source": "legi_bulk",
        **links,
    }
