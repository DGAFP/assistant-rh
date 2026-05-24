"""
Citation Extractor – Extract and match legal references from LLM responses.

This module detects article/decree mentions in generated responses and matches
them with available references from V3 context items to build source pills
linking to Légifrance.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Set

# ═══════════════════════════════════════════════════════════════════════════════
# DÉCRETS CONNUS - Mapping vers URLs Légifrance
# ═══════════════════════════════════════════════════════════════════════════════

KNOWN_DECREES = {
    # Décret n° 86-83 du 17 janvier 1986 - Agents contractuels de l'État
    "86-83": {
        "url": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000699956/",
        "title": "Décret n° 86-83 (agents contractuels)",
        "full_title": "Décret n° 86-83 du 17 janvier 1986 relatif aux agents contractuels de l'État",
    },
    # Ajouter d'autres décrets ici au besoin
    # "84-16": {
    #     "url": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000501099/",
    #     "title": "Décret n° 84-16 (dispositions statutaires FPE)",
    # },
}


@dataclass
class MatchedReference:
    """A legal reference matched from the response."""
    number: str           # e.g., "L332-2" or "86-83" for decrees
    cid: str              # Légifrance CID (or decree number for decrees)
    title: str            # e.g., "Code général de la fonction publique"
    url: str              # Full Légifrance URL
    is_decree: bool = False  # True if this is a full decree link (not an article)
    
    @property
    def display_title(self) -> str:
        """Title for display in pills."""
        if self.is_decree:
            return self.title  # Already formatted nicely
        # Truncate title if too long
        title_short = self.title[:30] + "..." if len(self.title) > 30 else self.title
        return f"Art. {self.number} - {title_short}"


def extract_article_mentions(text: str) -> Set[str]:
    """
    Extract article mentions from a text (typically LLM response).
    
    Detects patterns like:
    - L332-2, L. 332-2, L.332-2
    - Article L332-2, article L. 332-2
    - R5221-26, D2019-1414, A123-4
    
    Note: Some LLMs use non-breaking hyphens (U+2011 ‑) instead of regular hyphens.
    We normalize these before extraction.
    
    Args:
        text: Text to parse (LLM response)
    
    Returns:
        Set of normalized article numbers (e.g., {"L332-2", "L332-7"})
    """
    mentions = set()
    
    # Normalize: replace non-breaking hyphens (U+2011) and en-dashes (U+2013) with regular hyphens
    normalized_text = text.replace('‑', '-').replace('–', '-')
    
    # Pattern CGFP (L.xxx-xx) - most common
    for match in re.finditer(r'(?:article\s+)?L\.?\s*(\d{3}-\d+)', normalized_text, re.IGNORECASE):
        mentions.add(f"L{match.group(1)}")
    
    # Pattern other codes (R, D, A)
    for match in re.finditer(r'(?:article\s+)?([RDA])\.?\s*(\d{3,4}-\d+)', normalized_text, re.IGNORECASE):
        mentions.add(f"{match.group(1).upper()}{match.group(2)}")
    
    return mentions


def normalize_article_number(number: str) -> str:
    """
    Normalize an article number for matching.
    
    Examples:
        "L. 332-2" → "L332-2"
        "L332-2" → "L332-2"
        "L 332-2" → "L332-2"
    """
    return number.replace(" ", "").replace(".", "")


def build_legifrance_url(cid: str) -> str:
    """
    Build Légifrance URL for an article.
    
    Args:
        cid: Légifrance article CID (e.g., "LEGIARTI000044426716")
    
    Returns:
        Full URL to the article on Légifrance
    """
    return f"https://www.legifrance.gouv.fr/codes/article_lc/{cid}"


def extract_decree_mentions(text: str) -> Set[str]:
    """
    Extract decree mentions from a text (typically LLM response).
    
    Detects patterns like:
    - décret n° 86-83
    - décret 86-83
    - décret n°86-83
    - article 45 du décret n° 86-83
    
    Args:
        text: Text to parse (LLM response)
    
    Returns:
        Set of decree numbers (e.g., {"86-83"})
    """
    mentions = set()
    
    # Pattern: décret (n°)? XX-XXXX
    # The "n°" is optional (with or without space)
    # Captures: "86-83", "84-16", "91-155", "2019-1414", etc.
    pattern = r'décret\s*(?:n°\s*)?(\d{2,4}[-/]\d+)'
    
    for match in re.finditer(pattern, text, re.IGNORECASE):
        # Normalize: replace / with -
        decree_num = match.group(1).replace("/", "-")
        mentions.add(decree_num)
    
    return mentions


def check_decree_articles_in_sources(
    decree_num: str,
    matched_refs: List[MatchedReference],
    chunks: List,
) -> bool:
    """
    Check if any article from a decree is already in the sources.
    
    This prevents adding a pill to the full decree if we already have
    specific articles from that decree linked.
    
    Args:
        decree_num: Decree number (e.g., "86-83")
        matched_refs: Already matched article references
        chunks: Retrieved chunks (may contain DGAFP articles)
    
    Returns:
        True if an article from this decree is already in sources
    """
    # Check in matched refs (from expanded chunks)
    for ref in matched_refs:
        # If the ref URL contains the decree's JORF ID, it's from that decree
        decree_info = KNOWN_DECREES.get(decree_num)
        if decree_info:
            # Extract JORF ID from the decree URL
            # e.g., "JORFTEXT000000699956" from the full URL
            jorf_match = re.search(r'JORFTEXT\d+', decree_info.get("url", ""))
            if jorf_match:
                jorf_id = jorf_match.group(0)
                if jorf_id in ref.url:
                    return True
    
    # For now, we don't check chunks because DGAFP articles use LEGIARTI IDs
    # not JORFTEXT IDs, making cross-referencing complex.
    # The matched_refs check should be sufficient.
    
    return False


def match_refs_with_response_v3(
    response: str,
    legal_refs: List[Dict],
) -> List[MatchedReference]:
    """
    Match article mentions in response with V3 legal refs.
    
    This is the V3 version that uses the expanded legal_refs from the pipeline.
    The legal_refs already have 'cid' from the DGAFP expansion.
    
    Args:
        response: LLM-generated response text
        legal_refs: List of ref dicts from V3 pipeline (with number, cid, title, text)
    
    Returns:
        List of MatchedReference objects ready for display as pills
    """
    matched = []
    
    # 1. Extract article mentions from response
    mentions = extract_article_mentions(response)
    
    # 2. Match mentions with available refs (that have CID)
    if mentions and legal_refs:
        seen_cids = set()
        
        for ref in legal_refs:
            number = ref.get("number", "")
            cid = ref.get("cid", "")
            title = ref.get("title", "")
            
            # Skip refs without CID (can't link to Légifrance)
            if not cid or cid in seen_cids:
                continue
            
            # Normalize for matching
            normalized = normalize_article_number(number)
            
            if normalized in mentions:
                seen_cids.add(cid)
                matched.append(MatchedReference(
                    number=normalized,
                    cid=cid,
                    title=title or "Code général de la fonction publique",
                    url=build_legifrance_url(cid),
                    is_decree=False,
                ))
    
    # 3. Extract decree mentions and add links for decrees without specific articles
    decree_mentions = extract_decree_mentions(response)
    
    for decree_num in decree_mentions:
        # Only process known decrees
        if decree_num not in KNOWN_DECREES:
            continue
        
        # Check if we already have articles from this decree
        if check_decree_articles_in_sources(decree_num, matched, []):
            continue
        
        # Add the full decree link
        decree_info = KNOWN_DECREES[decree_num]
        matched.append(MatchedReference(
            number=decree_num,
            cid=f"DECREE-{decree_num}",
            title=decree_info["title"],
            url=decree_info["url"],
            is_decree=True,
        ))
    
    # Sort: articles first (by number), then decrees
    matched.sort(key=lambda r: (r.is_decree, r.number))
    
    return matched

