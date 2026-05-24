"""
Module pour dédupliquer et renommer les citations dans les réponses du LLM

Ce module résout le problème suivant :
- Le LLM reçoit des chunks avec des numéros [1], [2], [3], [4]
- Plusieurs chunks peuvent venir du même document
- On veut dédupliquer les sources affichées
- Et renommer les citations en conséquence

Exemple :
    Chunks : [MATTE, Décret, Code FP, MATTE]
    LLM cite : [1], [2], [4]
    Après déduplication : [1], [2], [1]
    Sources affichées : 1-MATTE, 2-Code FP
"""

import re
from typing import Dict, List, Tuple

from assistant_rh_rag_pipeline.models import Chunk


def deduplicate_sources(chunks: List[Chunk]) -> Tuple[List[Dict], Dict[int, int]]:
    """
    Déduplique les sources et crée un mapping chunk_index → source_index
    
    Args:
        chunks: Liste des chunks récupérés par le RAG
        
    Returns:
        Tuple de :
        - Liste des sources dédupliquées (dict avec title, url, is_internal_pdf)
        - Mapping {chunk_index: source_index} (1-based)
    
    Example:
        chunks = [chunk_matte_1, chunk_decret, chunk_fp, chunk_matte_2]
        sources, mapping = deduplicate_sources(chunks)
        # sources = [{"title": "MATTE", ...}, {"title": "Code FP", ...}]
        # mapping = {1: 1, 2: 2, 3: 3, 4: 1}  # chunk 4 → source 1 (dédupliqué)
    """
    sources = []
    chunk_to_source = {}  # {chunk_index: source_index}
    
    # Pour déduplication
    seen_sources = {}  # {source_key: source_index}
    
    for chunk_idx, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata or {}
        
        # Générer une clé unique pour identifier la source
        source_key = _get_source_key(chunk, meta)
        
        # Vérifier si on a déjà cette source
        if source_key in seen_sources:
            # Réutiliser l'index existant
            chunk_to_source[chunk_idx] = seen_sources[source_key]
        else:
            # Nouvelle source
            source_info = _extract_source_info(chunk, meta)
            sources.append(source_info)
            source_idx = len(sources)  # Index 1-based
            seen_sources[source_key] = source_idx
            chunk_to_source[chunk_idx] = source_idx
    
    return sources, chunk_to_source


def _get_source_key(chunk: Chunk, meta: dict) -> str:
    """
    Génère une clé unique pour identifier une source
    
    Logique de déduplication :
    - MATTE : Par source_document_id (même PDF)
    - DGAFP : Par numéro d'article
    - Service Public : Par SID (fiche)
    - Autres : Par URL
    """
    # MATTE avec document lié
    source_document_id = meta.get("source_document_id")
    if source_document_id:
        return f"matte_doc_{source_document_id}"
    
    # DGAFP : Par numéro d'article
    number = meta.get("number", "")
    cid = meta.get("cid", "")
    if number:
        return f"dgafp_{number}"
    
    # Service Public : Par SID
    sid = meta.get("sid", "")
    if sid:
        return f"sp_{sid}"
    
    # MATTE sans document : Par source_name
    source_name = meta.get("source_name", "")
    if source_name:
        return f"matte_name_{source_name}"
    
    # Par URL
    url = meta.get("url", "")
    if url:
        return f"url_{url}"
    
    # Fallback : texte du chunk (peu probable)
    return f"text_{hash(chunk.text[:100])}"


def _extract_source_info(chunk: Chunk, meta: dict) -> Dict:
    """
    Extrait les infos d'affichage d'une source
    
    Returns:
        Dict avec title, url, is_internal_pdf
    """
    from src.ui.document_url_helper import get_document_url
    
    cid = meta.get("cid", "")
    number = meta.get("number", "")
    sid = meta.get("sid", "")
    
    url = meta.get("url") or ""
    is_internal_pdf = False
    
    # MATTE : Vérifier si un document PDF est lié
    source_document_id = meta.get("source_document_id")
    if source_document_id and not url:
        url = get_document_url(source_document_id, relative=True, pdf_only=True)
        is_internal_pdf = True
    
    # Générer URL Légifrance si cid présent
    if cid and not url:
        url = f"https://www.legifrance.gouv.fr/codes/id/{cid}/"
    
    # Titre selon la source
    if cid:  # DGAFP
        title = (meta.get("full_title") or meta.get("title") or "").strip()
        if not title and meta.get("number"):
            nature = meta.get("nature", "Article")
            title = f"{nature} {meta.get('number')}"
        if not title:
            title = "Code Général FP"
    elif sid:  # Service Public
        title = (meta.get("title") or "").strip()
        if not title:
            title = f"Fiche {sid}"
    else:  # MATTE ou autre
        title = (meta.get("source_name") or meta.get("source") or "").strip()
        if not title:
            title = chunk.preview(30)
        if not title or title == "…":
            if url:
                import re
                url_match = re.search(r'/([A-Z]\d+)$', url)
                if url_match:
                    title = f"Fiche {url_match.group(1)}"
                elif "legifrance" in url:
                    title = "Légifrance"
                else:
                    title = "Source externe"
            else:
                title = "Source"
    
    # Tronquer le titre si trop long
    if len(title) > 40:
        title = title[:37] + "..."
    
    return {
        "title": title,
        "url": url,
        "is_internal_pdf": is_internal_pdf
    }


def renumber_citations(answer: str, mapping: Dict[int, int]) -> str:
    """
    Renomme les citations [X] dans la réponse du LLM selon le mapping
    
    Args:
        answer: Réponse du LLM avec citations [1], [2], etc.
        mapping: Dict {old_index: new_index}
        
    Returns:
        Réponse avec citations renommées
        
    Example:
        answer = "Pour recruter [1]... selon [2]... et [4]"
        mapping = {1: 1, 2: 2, 4: 1}
        result = "Pour recruter [1]... selon [2]... et [1]"
    """
    # Pattern pour détecter les citations [X]
    pattern = r'\[(\d+)\]'
    
    def replace_citation(match):
        old_idx = int(match.group(1))
        new_idx = mapping.get(old_idx, old_idx)  # Fallback sur old si pas dans mapping
        return f'[{new_idx}]'
    
    return re.sub(pattern, replace_citation, answer)


def filter_used_sources(
    sources: List[Dict], 
    chunk_to_source: Dict[int, int], 
    answer: str
) -> Tuple[List[Dict], str]:
    """
    Filtre les sources pour ne garder que celles utilisées dans la réponse
    et renomme les citations en conséquence
    
    Args:
        sources: Liste de toutes les sources dédupliquées
        chunk_to_source: Mapping {chunk_index: source_index}
        answer: Réponse du LLM
        
    Returns:
        Tuple de :
        - Liste des sources utilisées
        - Réponse avec citations renommées
        
    Example:
        sources = [source1, source2, source3]
        chunk_to_source = {1: 1, 2: 2, 3: 3, 4: 1}
        answer = "Text [1]... et [4]"  # Utilise chunks 1 et 4
        
        result_sources, result_answer = filter_used_sources(...)
        # result_sources = [source1]  # Seulement source 1
        # result_answer = "Text [1]... et [1]"  # [4] renommé en [1]
    """
    # Détecter quels chunks sont cités dans la réponse
    cited_chunks = set(re.findall(r'\[(\d+)\]', answer))
    cited_chunks = {int(x) for x in cited_chunks}
    
    if not cited_chunks:
        # Pas de citations détectées, retourner tout
        return sources, answer
    
    # Trouver quelles sources sont utilisées
    used_sources_idx = set()
    for chunk_idx in cited_chunks:
        if chunk_idx in chunk_to_source:
            source_idx = chunk_to_source[chunk_idx]
            used_sources_idx.add(source_idx)
    
    # Créer la liste filtrée des sources
    used_sources_idx_sorted = sorted(used_sources_idx)
    filtered_sources = [sources[idx - 1] for idx in used_sources_idx_sorted]  # -1 car 1-based
    
    # Créer un nouveau mapping : old_source_idx → new_source_idx
    source_renumbering = {old: new + 1 for new, old in enumerate(used_sources_idx_sorted)}
    
    # Créer le mapping final : chunk_idx → new_source_idx
    final_mapping = {}
    for chunk_idx, old_source_idx in chunk_to_source.items():
        if old_source_idx in source_renumbering:
            final_mapping[chunk_idx] = source_renumbering[old_source_idx]
    
    # Renommer les citations
    new_answer = renumber_citations(answer, final_mapping)
    
    return filtered_sources, new_answer


# Fonction principale (tout-en-un)
def process_answer_and_sources(
    answer: str, 
    chunks: List[Chunk]
) -> Tuple[str, List[Dict]]:
    """
    Fonction principale : déduplique, filtre et renomme tout en une fois
    
    Args:
        answer: Réponse du LLM avec citations
        chunks: Chunks utilisés pour la réponse
        
    Returns:
        Tuple de :
        - Réponse avec citations renommées
        - Liste des sources dédupliquées et filtrées
        
    Example:
        answer = "Pour recruter [1]... selon [2]... et [4]"
        chunks = [chunk_matte_1, chunk_decret, chunk_fp, chunk_matte_2]
        
        new_answer, sources = process_answer_and_sources(answer, chunks)
        # new_answer = "Pour recruter [1]... selon [2]... et [1]"
        # sources = [{"title": "MATTE", ...}, {"title": "Code FP", ...}]
    """
    # 1. Dédupliquer les sources
    all_sources, chunk_to_source = deduplicate_sources(chunks)
    
    # 2. Filtrer les sources utilisées et renommer
    filtered_sources, new_answer = filter_used_sources(
        all_sources, 
        chunk_to_source, 
        answer
    )
    
    return new_answer, filtered_sources


