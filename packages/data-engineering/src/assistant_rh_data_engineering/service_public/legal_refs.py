"""
Legal References - Extraction et parsing des références juridiques.

Module centralisé pour :
1. Extraire les références juridiques d'un texte
2. Les parser en structure normalisée
3. Les résoudre vers des chunks existants (rag_chunks_dgafp)
4. Stocker les références manquantes (legal_ref_fragments)

Usage:
    from assistant_rh_data_engineering.service_public.legal_refs import (
        extract_legal_refs,
        parse_legal_ref,
        resolve_legal_refs,
        LegalRef,
    )

    # Extraction brute
    refs_raw = extract_legal_refs(text)  # ["L. 332-2 1°", "décret n°86-83"]

    # Parsing structuré
    refs_parsed = [parse_legal_ref(r) for r in refs_raw]

    # Résolution avec CID (nécessite connexion DB)
    refs_resolved = resolve_legal_refs(refs_parsed, db_connection)
"""

import logging
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENUMS & DATA CLASSES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RefSource(str, Enum):
    """Source de la référence juridique (quelle table consulter)."""
    DGAFP = "DGAFP"      # rag_chunks_dgafp
    MATTE = "MATTE"      # rag_chunks_matte (annexes, fiches internes)
    SERVICE_PUBLIC = "SERVICE_PUBLIC"  # rag_chunks_fiches_sp
    UNKNOWN = "UNKNOWN"


class RefCategory(str, Enum):
    """Catégorie de texte juridique."""
    CODE = "CODE"        # Code Général de la Fonction Publique (L.XXX-X)
    DECRET = "DECRET"    # Décrets (n°XX-XXX)
    ARRETE = "ARRETE"    # Arrêtés
    CIRCULAIRE = "CIRCULAIRE"
    ANNEXE = "ANNEXE"    # Annexe interne MATTE
    FICHE = "FICHE"      # Fiche interne MATTE
    OTHER = "OTHER"


@dataclass
class LegalRef:
    """
    Référence juridique structurée.

    Attributs:
        source: Table de référence (DGAFP, MATTE, etc.)
        category: Type de texte (CODE, DECRET, ANNEXE, etc.)
        number: Numéro normalisé (L332-7, A5, F8, etc.)
        title: Titre complet du document
        paragraph: Paragraphe/alinéa spécifique (1°, III, a), etc.)
        decret_number: Numéro du décret si applicable
        cid: ID du chunk résolu dans la table source
        url: URL Légifrance si connue
        fragment_text: Texte du fragment (si extrait)
        status: État de validité (VIGUEUR, ABROGE, etc.)
        raw_text: Texte brut original de la référence
        resolved: True si la référence a été résolue vers un chunk
    """
    source: str = ""
    category: str = ""
    number: str = ""
    title: str = ""
    paragraph: str = ""
    decret_number: str = ""
    cid: str = ""
    url: str = ""
    fragment_text: str = ""
    status: str = ""
    raw_text: str = ""
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convertit en dict pour JSON."""
        result: Dict[str, Any] = {}
        for key, value in asdict(self).items():
            if value is None or value == "":
                continue
            result[key] = value
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LegalRef":
        """Crée depuis un dict."""
        fields = cls.__dataclass_fields__
        values: Dict[str, Any] = {}
        for key, value in data.items():
            if key in fields:
                values[key] = value
        return cls(**values)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PATTERNS REGEX
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Patterns d'extraction (ordre important: du plus spécifique au plus général)
EXTRACTION_PATTERNS = [
    # Art. L.332-2 2° a) ou Art. L.332-2 2° b) - avec lettre et parenthèse
    r'Art(?:icle)?\.?\s*L\.?\s*\d{3}-\d+(?:\s+\d+°(?:\s+[a-z]\))?)?',
    # L. 332-2 1° ou L. 332-2 2° ou L.332-22 - avec ou sans paragraphe
    r'L\.?\s*\d{3}-\d+(?:\s+\d+°(?:\s+[a-z]\))?)?',
    # R. 123-4 (articles réglementaires)
    r'R\.?\s*\d{3}-\d+(?:\s+\d+°)?',
    # D. 123-4 (articles de décret dans le code)
    r'D\.?\s*\d{3}-\d+(?:\s+\d+°)?',
    # article 34 du décret n°86-83 du 17 janvier 1986
    r'article\s+\d+\s+du\s+décret\s+n°\s*\d{2,4}-\d+(?:\s+du\s+\d+(?:\s+\w+\s+\d+)?)?',
    # décret n°86-83 seul
    r'décret\s+n°\s*\d{2,4}-\d+',
    # III de l'article 2 du décret 2019-1414
    r'(?:I{1,3}V?|V?I{1,3})\s+de\s+l\'article\s+\d+\s+du\s+décret\s+\d{4}-\d+',
    # ANNEXE X ou Annexe X (avec ou sans point)
    r'ANNEXE\s+\d+\.?',
    # FICHE X ou Fiche X (avec ou sans point)
    r'FICHE\s+\d+\.?',
    # Liens Légifrance dans le Markdown
    r'\[([^\]]+)\]\((https?://www\.legifrance\.gouv\.fr[^\)]+)\)',
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXTRACTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_legal_refs(text: str) -> List[str]:
    """
    Extrait automatiquement les références juridiques d'un texte.

    Patterns détectés:
    - L. 332-2, L.332-22, L332-24
    - L. 332-2 1°, L. 332-2 2° a)
    - Art. L.332-2 2° a)
    - R. 123-4, D. 456-7
    - III de l'article 2 du décret 2019-1414
    - article 34 du décret n°86-83 du 17 janvier 1986
    - ANNEXE 5, FICHE 8

    Returns:
        Liste de références brutes dédupliquées
    """
    if not text:
        return []

    refs = []
    seen = set()

    for pattern in EXTRACTION_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            # Si c'est un tuple (cas des liens Markdown), prendre le texte
            if isinstance(match, tuple):
                ref = match[0].strip()  # Texte du lien
            else:
                ref = match.strip()

            # Normaliser pour déduplication
            ref_normalized = ref.lower().replace(' ', '').replace('.', '')

            if ref_normalized not in seen and ref:
                seen.add(ref_normalized)
                refs.append(ref)

    return refs


def extract_legifrance_urls(text: str) -> List[Dict[str, str]]:
    """
    Extrait les liens Légifrance avec leurs URLs.

    Returns:
        Liste de {titre, url}
    """
    pattern = r'\[([^\]]+)\]\((https?://www\.legifrance\.gouv\.fr[^\)]+)\)'
    matches = re.findall(pattern, text, re.IGNORECASE)

    results = []
    seen_urls = set()

    for titre, url in matches:
        if url not in seen_urls:
            seen_urls.add(url)
            results.append({"titre": titre.strip(), "url": url})

    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PARSING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_legal_ref(ref: str) -> LegalRef:
    """
    Parse une référence juridique et retourne un objet LegalRef structuré.

    Exemples:
    - "L. 332-2 1°" → LegalRef(source="DGAFP", category="CODE", number="L332-2", paragraph="1°")
    - "Art. L.332-7" → LegalRef(source="DGAFP", category="CODE", number="L332-7")
    - "article 34 du décret n°86-83" → LegalRef(source="DGAFP", category="DECRET", number="34", decret_number="86-83")
    - "ANNEXE 5" → LegalRef(source="MATTE", category="ANNEXE", number="A5")
    """
    result = LegalRef(raw_text=ref)
    ref_clean = ref.strip()

    # === PATTERN: article X du décret n°YY-ZZ ===
    article_decret_match = re.match(
        r'article\s+(\d+)\s+du\s+décret\s+n°\s*(\d{2,4}-\d+)',
        ref_clean, re.IGNORECASE
    )
    if article_decret_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.DECRET.value
        result.number = article_decret_match.group(1)
        result.decret_number = article_decret_match.group(2)
        return result

    # === PATTERN: III de l'article X du décret YYYY-ZZZZ ===
    decret_article_match = re.match(
        r'(I{1,3}V?|V?I{0,3})\s+de\s+l\'article\s+(\d+)\s+du\s+décret\s+(\d{4}-\d+)',
        ref_clean, re.IGNORECASE
    )
    if decret_article_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.DECRET.value
        result.number = decret_article_match.group(2)
        result.paragraph = decret_article_match.group(1).upper()
        result.decret_number = decret_article_match.group(3)
        return result

    # === PATTERN: décret n°XX-XX seul ===
    decret_match = re.match(r'décret\s+n°\s*(\d{2,4}-\d+)', ref_clean, re.IGNORECASE)
    if decret_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.DECRET.value
        result.decret_number = decret_match.group(1)
        return result

    # === PATTERN: Articles du CGFP (L.XXX-XX avec paragraphe optionnel) ===
    cgfp_match = re.match(
        r'(?:Art(?:icle)?\.?\s*)?L\.?\s*(\d{3})-(\d+)(?:\s+(\d+°)(?:\s+([a-z]\)))?)?',
        ref_clean, re.IGNORECASE
    )
    if cgfp_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.CODE.value
        result.number = f"L{cgfp_match.group(1)}-{cgfp_match.group(2)}"
        if cgfp_match.group(3):
            paragraph = cgfp_match.group(3)
            if cgfp_match.group(4):
                paragraph += " " + cgfp_match.group(4)
            result.paragraph = paragraph
        return result

    # === PATTERN: Articles réglementaires R.XXX-XX ===
    r_match = re.match(
        r'R\.?\s*(\d{3})-(\d+)(?:\s+(\d+°))?',
        ref_clean, re.IGNORECASE
    )
    if r_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.CODE.value
        result.number = f"R{r_match.group(1)}-{r_match.group(2)}"
        if r_match.group(3):
            result.paragraph = r_match.group(3)
        return result

    # === PATTERN: Articles décret D.XXX-XX ===
    d_match = re.match(
        r'D\.?\s*(\d{3})-(\d+)(?:\s+(\d+°))?',
        ref_clean, re.IGNORECASE
    )
    if d_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.CODE.value
        result.number = f"D{d_match.group(1)}-{d_match.group(2)}"
        if d_match.group(3):
            result.paragraph = d_match.group(3)
        return result

    # === PATTERN: "article XX" simple ===
    article_match = re.match(r'article\s+(\d+)$', ref_clean, re.IGNORECASE)
    if article_match:
        result.source = RefSource.DGAFP.value
        result.category = RefCategory.DECRET.value
        result.number = article_match.group(1)
        return result

    # === PATTERN: ANNEXE X ===
    annexe_match = re.match(r'ANNEXE\s+(\d+)\.?', ref_clean, re.IGNORECASE)
    if annexe_match:
        result.source = RefSource.MATTE.value
        result.category = RefCategory.ANNEXE.value
        result.number = f"A{annexe_match.group(1)}"
        return result

    # === PATTERN: FICHE X ===
    fiche_match = re.match(r'FICHE\s+(\d+)\.?', ref_clean, re.IGNORECASE)
    if fiche_match:
        result.source = RefSource.MATTE.value
        result.category = RefCategory.FICHE.value
        result.number = f"F{fiche_match.group(1)}"
        return result

    # === FALLBACK ===
    result.source = RefSource.UNKNOWN.value
    result.category = RefCategory.OTHER.value
    result.number = ref_clean
    return result


def categorize_legal_ref(ref: str) -> str:
    """
    Catégorise rapidement une référence par type.

    Returns: "CODE", "DECRET", "MATTE", "SERVICE_PUBLIC" ou "AUTRE"
    """
    ref_lower = ref.lower()

    if 'annexe' in ref_lower or 'fiche' in ref_lower:
        return "MATTE"
    elif any(x in ref_lower for x in ['l.332', 'l332', 'l 332', 'r.', 'd.']):
        return "CODE"
    elif 'décret' in ref_lower:
        return "DECRET"
    elif 'article' in ref_lower and 'l.' not in ref_lower:
        return "DECRET"
    elif 'l.' in ref_lower or 'l ' in ref_lower:
        return "CODE"
    else:
        return "AUTRE"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESOLUTION (vers chunks existants)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def resolve_legal_ref(ref: LegalRef, cursor) -> LegalRef:
    """
    Résout une référence juridique vers un chunk existant.

    Cherche dans rag_chunks_dgafp selon source/category/number.
    Met à jour ref.cid et ref.resolved si trouvé.

    Args:
        ref: LegalRef à résoudre
        cursor: Cursor DB (psycopg ou autre)

    Returns:
        LegalRef mis à jour avec cid/resolved
    """
    if ref.source != RefSource.DGAFP.value:
        return ref  # Seules les refs DGAFP sont résolubles pour l'instant

    try:
        # Cas 1: Article de décret spécifique
        if ref.decret_number and ref.number:
            patterns = [
                f"%{ref.decret_number}%",
                f"%décret%{ref.decret_number}%",
                f"%n°{ref.decret_number}%",
            ]

            for pattern in patterns:
                cursor.execute("""
                    SELECT cid, chunk_text, title, number, status
                    FROM rag_chunks_dgafp
                    WHERE title ILIKE %s AND number = %s
                    LIMIT 1
                """, (pattern, ref.number))

                result = cursor.fetchone()
                if result:
                    ref.cid = result[0]
                    ref.title = result[2] or ""
                    ref.status = result[4] or ""
                    ref.resolved = True
                    return ref

        # Cas 2: Article du CGFP (L332-X, R123-X, D456-X)
        if ref.category == RefCategory.CODE.value and ref.number:
            # Recherche exacte d'abord
            cursor.execute("""
                SELECT cid, chunk_text, title, number, status
                FROM rag_chunks_dgafp
                WHERE number = %s
                LIMIT 1
            """, (ref.number,))

            result = cursor.fetchone()
            if result:
                ref.cid = result[0]
                ref.title = result[2] or ""
                ref.status = result[4] or ""
                ref.resolved = True
                return ref

            # Recherche fuzzy (sans tiret, etc.)
            number_clean = ref.number.replace("-", "").replace(" ", "")
            cursor.execute("""
                SELECT cid, chunk_text, title, number, status
                FROM rag_chunks_dgafp
                WHERE REPLACE(REPLACE(number, '-', ''), ' ', '') = %s
                LIMIT 1
            """, (number_clean,))

            result = cursor.fetchone()
            if result:
                ref.cid = result[0]
                ref.title = result[2] or ""
                ref.status = result[4] or ""
                ref.resolved = True
                return ref

    except Exception:
        logger.exception("Erreur résolution référence juridique %s", ref.number)

    return ref


def resolve_legal_refs(refs: List[LegalRef], cursor) -> List[LegalRef]:
    """
    Résout une liste de références vers des chunks existants.

    Args:
        refs: Liste de LegalRef à résoudre
        cursor: Cursor DB

    Returns:
        Liste de LegalRef mis à jour
    """
    return [resolve_legal_ref(ref, cursor) for ref in refs]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONVENIENCE FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_and_parse_refs(text: str) -> List[LegalRef]:
    """
    Extrait et parse les références juridiques d'un texte en une seule opération.

    Args:
        text: Texte à analyser

    Returns:
        Liste de LegalRef structurés
    """
    raw_refs = extract_legal_refs(text)
    return [parse_legal_ref(ref) for ref in raw_refs]


def refs_to_json(refs: List[LegalRef]) -> List[Dict[str, Any]]:
    """Convertit une liste de LegalRef en JSON-serializable."""
    return [ref.to_dict() for ref in refs]


def refs_from_json(data: List[Dict[str, Any]]) -> List[LegalRef]:
    """Crée une liste de LegalRef depuis du JSON."""
    return [LegalRef.from_dict(d) for d in data]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STATS & DEBUG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_refs_summary(refs: List[LegalRef]) -> Dict[str, Any]:
    """Retourne un résumé des références."""
    by_source = {}
    by_category = {}
    resolved_count = 0

    for ref in refs:
        by_source[ref.source] = by_source.get(ref.source, 0) + 1
        by_category[ref.category] = by_category.get(ref.category, 0) + 1
        if ref.resolved:
            resolved_count += 1

    return {
        "total": len(refs),
        "resolved": resolved_count,
        "unresolved": len(refs) - resolved_count,
        "by_source": by_source,
        "by_category": by_category,
    }
