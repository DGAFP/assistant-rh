"""
XML Parser V3 pour les fiches Service-Public

Adapté au nouveau workflow RAG V3 :
1. Parse XML → doc_markdown (document entier)
2. Extrait les métadonnées pour rag_documents
3. PAS de chunking - c'est fait par section_splitter + chunker

Usage:
    from assistant_rh_data_engineering.service_public.xml_parser import parse_fiche_xml

    result = parse_fiche_xml("F515", xml_dir)
    # result = {
    #     "doc_markdown": "...",
    #     "metadata": {...},
    #     "title": "...",
    #     ...
    # }
"""

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ============================================================================
# CONFIGURATION
# ============================================================================

XML_PARSER_VERSION = "v3.0"
logger = logging.getLogger(__name__)


# ============================================================================
# FONCTIONS UTILITAIRES - TEXTE
# ============================================================================


def normalize_text_spaces(text: str) -> str:
    """Normalise les espaces typographiques invisibles du XML Service-Public."""
    return text.replace("\xa0", " ").replace("\u202f", " ")


def clean_markdown(text: str) -> str:
    """Nettoie le texte Markdown extrait."""
    if not text:
        return ""

    text = normalize_text_spaces(text)

    # Supprimer les espaces multiples (mais pas les sauts de ligne)
    text = re.sub(r" +", " ", text)

    # Normaliser les sauts de ligne (max 2 consécutifs)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Supprimer les espaces en début/fin de ligne
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    # Supprimer les lignes vides au début et à la fin
    text = text.strip()

    # Nettoyer les espaces autour de la ponctuation
    text = re.sub(r"\s+([.,;:!?\)])", r"\1", text)
    text = re.sub(r"(\()\s+", r"\1", text)

    return text


def extract_text_simple(element: Optional[ET.Element]) -> str:
    """Extrait le texte brut d'un élément (pour les titres, etc.)."""
    if element is None:
        return ""

    texts = []

    if element.text:
        texts.append(element.text.strip())

    for child in element:
        child_text = extract_text_simple(child)
        if child_text:
            texts.append(child_text)
        if child.tail:
            texts.append(child.tail.strip())

    text = normalize_text_spaces(" ".join(t for t in texts if t))
    return re.sub(r" +", " ", text).strip()


def compute_text_hash(text: str) -> str:
    """Calcule le hash SHA256 d'un texte."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def count_tokens(text: str) -> int:
    """Estimation du nombre de tokens (~4.2 chars/token en français)."""
    if not text:
        return 0
    return max(1, int(len(text) / 4.2))


def extract_definitions_from_root(root: ET.Element) -> Dict[str, str]:
    """Extrait toutes les définitions d'une fiche XML."""
    definitions = {}
    for defn in root.findall(".//Definition"):
        id_def = defn.get("ID", "")
        if id_def:
            texte_elem = defn.find("Texte")
            if texte_elem is not None:
                texte = "".join(texte_elem.itertext()).strip()
                if texte:
                    definitions[id_def] = texte
    return definitions


# ============================================================================
# EXTRACTION MARKDOWN
# ============================================================================


def extract_table_markdown(tableau_elem: ET.Element) -> str:
    """Extrait un tableau XML en format Markdown."""
    if tableau_elem is None:
        return ""

    lines = []

    # Titre du tableau
    titre = tableau_elem.find("Titre")
    if titre is not None:
        titre_text = "".join(titre.itertext()).strip()
        if titre_text:
            lines.append(f"\n**📊 {titre_text}**\n")

    # Extraire les rangées
    rangees = tableau_elem.findall("Rangée")
    if not rangees:
        return ""

    # Première rangée = en-têtes
    headers = []
    for cell in rangees[0].findall("Cellule"):
        cell_text = "".join(cell.itertext()).strip()
        cell_text = cell_text.replace("\n", " ").replace("|", "/")
        headers.append(cell_text)

    if not headers:
        return ""

    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---" for _ in headers]) + "|")

    # Lignes de données
    for rangee in rangees[1:]:
        cells = []
        for cell in rangee.findall("Cellule"):
            cell_text = "".join(cell.itertext()).strip()
            cell_text = cell_text.replace("\n", " ").replace("|", "/")
            cells.append(cell_text)

        while len(cells) < len(headers):
            cells.append("")

        lines.append("| " + " | ".join(cells[: len(headers)]) + " |")

    return "\n".join(lines) + "\n"


def extract_list_markdown(liste_elem: ET.Element) -> str:
    """Extrait une liste en format Markdown."""
    list_items = []

    for item in liste_elem.findall("Item"):
        item_texts = []
        for para in item.findall(".//Paragraphe"):
            para_text = extract_text_simple(para)
            if para_text:
                item_texts.append(para_text.strip())

        if not item_texts:
            item_text = extract_text_simple(item)
            if item_text:
                item_texts.append(item_text.strip())

        if item_texts:
            item_text = " ".join(item_texts)
            list_items.append(f"- {item_text}")

    if list_items:
        return "\n" + "\n".join(list_items) + "\n"
    return ""


def _extract_inner_text(element: ET.Element, definitions: Dict[str, str] | None = None) -> str:
    """Extrait le texte intérieur d'un élément note."""
    parts = []
    if element.text:
        parts.append(element.text.strip())
    for child in element:
        if child.tag == "Titre":
            continue
        child_text = extract_text_markdown(child, definitions=definitions)
        if child_text:
            parts.append(child_text.strip())
        if child.tail:
            parts.append(child.tail.strip())
    return " ".join(parts)


def extract_text_markdown(element: Optional[ET.Element], level: int = 0, definitions: Dict[str, str] | None = None) -> str:
    """
    Extrait le texte d'un élément XML en préservant le format Markdown.

    Conversions :
    - <Liste> → liste à puces
    - <MiseEnEvidence> → **gras**
    - <Paragraphe> → séparés par \n\n
    - <ASavoir> → 💡 **À savoir :**
    - <ANoter> → 📝 **À noter :**
    - <Attention> → ⚠️ **Attention :**
    - <Rappel> → 📌 **Rappel :**
    - <Tableau> → format Markdown table
    """
    if element is None:
        return ""

    # Cas spéciaux
    if element.tag == "Tableau":
        return extract_table_markdown(element)

    if element.tag == "Liste":
        return extract_list_markdown(element)

    # ServiceEnLigne - transformer en lien
    if element.tag == "ServiceEnLigne":
        titre_elem = element.find("Titre")
        url = element.get("URL", "")
        titre = extract_text_simple(titre_elem) if titre_elem is not None else ""
        if titre and url:
            return f"\n\n🌐 [{titre}]({url})\n\n"
        elif titre:
            return f"\n\n🌐 {titre}\n\n"
        return ""

    if element.tag == "BlocCas":
        header_prefix = "###" if level == 0 else "####"

        cas_list = element.findall("Cas")
        if cas_list:
            cas_texts = []
            for cas in cas_list:
                cas_titre_elem = cas.find("Titre")
                cas_titre = extract_text_simple(cas_titre_elem) if cas_titre_elem is not None else None

                cas_content_parts = []
                for cas_child in cas:
                    if cas_child.tag != "Titre":
                        cas_child_text = extract_text_markdown(cas_child, level + 1, definitions)
                        if cas_child_text:
                            cas_content_parts.append(cas_child_text.strip())

                cas_content = "\n\n".join(cas_content_parts)

                if cas_titre and cas_content:
                    cas_texts.append(f"{header_prefix} {cas_titre}\n\n{cas_content}")
                elif cas_titre:
                    cas_texts.append(f"{header_prefix} {cas_titre}")
                elif cas_content:
                    cas_texts.append(f"{cas_content}")

            if cas_texts:
                return "\n\n" + "\n\n".join(cas_texts) + "\n"
        return ""

    # Notes spéciales
    if element.tag == "Attention":
        inner_text = _extract_inner_text(element, definitions)
        return f"\n\n⚠️ **Attention :** {inner_text.strip()}\n\n" if inner_text else ""

    if element.tag == "ASavoir":
        inner_text = _extract_inner_text(element, definitions)
        return f"\n\n💡 **À savoir :** {inner_text.strip()}\n\n" if inner_text else ""

    if element.tag == "ANoter":
        inner_text = _extract_inner_text(element, definitions)
        return f"\n\n📝 **À noter :** {inner_text.strip()}\n\n" if inner_text else ""

    if element.tag == "Rappel":
        inner_text = _extract_inner_text(element, definitions)
        return f"\n\n📌 **Rappel :** {inner_text.strip()}\n\n" if inner_text else ""

    if element.tag == "Exemple":
        inner_text = _extract_inner_text(element, definitions)
        return f"\n\n📋 **Exemple :** {inner_text.strip()}\n\n" if inner_text else ""

    parts = []

    if element.text:
        text = element.text.strip()
        if text:
            parts.append(text)

    for child in element:
        child_text = ""

        if child.tag == "Liste":
            child_text = "\n" + extract_list_markdown(child) + "\n"

        elif child.tag == "Paragraphe":
            para_text = extract_text_markdown(child, level + 1, definitions)
            if para_text:
                child_text = para_text.strip() + "\n\n"

        elif child.tag == "MiseEnEvidence":
            inner_text = extract_text_markdown(child, level + 1, definitions)
            if inner_text:
                child_text = f" **{inner_text.strip()}** "

        elif child.tag == "Valeur":
            inner_text = "".join(child.itertext()).strip()
            if inner_text:
                child_text = f" **{inner_text}** "

        elif child.tag in ["ASavoir", "ANoter", "Attention", "Rappel", "Exemple"]:
            # Ces éléments sont déjà formatés avec icône par extract_text_markdown
            # Ne pas ajouter l'icône une seconde fois
            child_text = extract_text_markdown(child, level + 1, definitions)

        elif child.tag == "Exposant":
            # Gérer les exposants (1er, 2e, etc.) avec un espace après
            inner_text = "".join(child.itertext()).strip()
            if inner_text:
                child_text = f"{inner_text} "

        elif child.tag == "TitreFlottant":
            inner_text = extract_text_markdown(child, level + 1, definitions)
            if inner_text:
                child_text = f"\n\n**{inner_text.strip()}**\n\n"

        elif child.tag == "LienInterne":
            inner_text = child.text or ""
            lien_id = child.get("LienPublication", "")
            if inner_text:
                if lien_id:
                    child_text = f" [{inner_text.strip()}]({lien_id}) "
                else:
                    child_text = f" {inner_text.strip()} "

        elif child.tag == "LienIntra":
            inner_text = child.text or ""
            lien_id = child.get("LienID", "")
            if inner_text:
                definition = definitions.get(lien_id) if lien_id and definitions else None
                if definition:
                    child_text = f" {inner_text.strip()} ({definition}) "
                else:
                    child_text = f" {inner_text.strip()} "

        elif child.tag == "LienExterne":
            inner_text = child.text or ""
            url = child.get("URL", "")
            if inner_text:
                if url:
                    child_text = f" [{inner_text.strip()}]({url}) "
                else:
                    child_text = f" {inner_text.strip()} "

        elif child.tag == "Expression":
            # Expression en italique avec espaces
            inner_text = "".join(child.itertext()).strip()
            if inner_text:
                child_text = f" _{inner_text}_ "

        elif child.tag == "ServiceEnLigne":
            # Transformer en lien markdown avec titre et URL
            titre_elem = child.find("Titre")
            url = child.get("URL", "")
            titre = extract_text_simple(titre_elem) if titre_elem is not None else ""
            if titre and url:
                child_text = f"\n\n🌐 [{titre}]({url})\n\n"
            elif titre:
                child_text = f"\n\n🌐 {titre}\n\n"
            # Note: On ignore volontairement le <Source> qui est juste informatif

        elif child.tag == "Source":
            # Ignoré (fait partie de ServiceEnLigne/Reference)
            pass

        elif child.tag == "Tableau":
            table_text = extract_table_markdown(child)
            if table_text:
                child_text = f"\n\n{table_text}\n"

        elif child.tag == "BlocCas":
            header_prefix = "###" if level == 0 else "####"

            cas_list = child.findall("Cas")
            if cas_list:
                cas_texts = []
                for cas in cas_list:
                    cas_titre_elem = cas.find("Titre")
                    cas_titre = extract_text_simple(cas_titre_elem) if cas_titre_elem is not None else None

                    cas_content_parts = []
                    for cas_child in cas:
                        if cas_child.tag != "Titre":
                            cas_child_text = extract_text_markdown(cas_child, level + 1, definitions)
                            if cas_child_text:
                                cas_content_parts.append(cas_child_text.strip())

                    cas_content = "\n\n".join(cas_content_parts)

                    if cas_titre and cas_content:
                        cas_texts.append(f"{header_prefix} {cas_titre}\n\n{cas_content}")
                    elif cas_titre:
                        cas_texts.append(f"{header_prefix} {cas_titre}")
                    elif cas_content:
                        cas_texts.append(f"{cas_content}")

                if cas_texts:
                    child_text = "\n\n" + "\n\n".join(cas_texts) + "\n"

        elif child.tag in ["Chapitre", "SousChapitre", "Cas", "OuSAdresser", "Titre"]:
            pass  # Ignorés ou traités ailleurs

        else:
            inner_text = extract_text_markdown(child, level + 1, definitions)
            if inner_text:
                child_text = inner_text

        if child_text:
            parts.append(child_text)

        if child.tail:
            tail = child.tail.strip()
            if tail:
                parts.append(tail)

    return "".join(parts)


# ============================================================================
# EXTRACTION MÉTADONNÉES
# ============================================================================


def extract_fil_ariane(root: ET.Element) -> str:
    """Extrait le fil d'Ariane."""
    fil_ariane = root.find(".//FilDAriane")
    if fil_ariane is None:
        return ""

    niveaux = []
    for niveau in fil_ariane.findall("Niveau"):
        titre = niveau.text
        if titre and titre not in ["Accueil particuliers", "Accueil"]:
            niveaux.append(titre.strip())

    return " > ".join(niveaux)


def extract_subtitles(root: ET.Element) -> str:
    """Extrait la hiérarchie thématique."""
    hierarchy = []

    theme = root.find(".//Theme/Titre")
    if theme is not None and theme.text:
        hierarchy.append(theme.text.strip())

    sous_theme = root.find(".//SousThemePere")
    if sous_theme is not None and sous_theme.text:
        hierarchy.append(sous_theme.text.strip())

    dossier = root.find(".//DossierPere/Titre")
    if dossier is not None and dossier.text:
        hierarchy.append(dossier.text.strip())

    sous_dossier = root.find(".//SousDossierPere")
    if sous_dossier is not None and sous_dossier.text:
        hierarchy.append(sous_dossier.text.strip())

    return " > ".join(hierarchy) if hierarchy else ""


def extract_liens_internes(root: ET.Element) -> List[Dict[str, str]]:
    """Extrait les liens vers d'autres fiches."""
    liens = []
    seen = set()

    for lien in root.findall(".//LienInterne"):
        lien_pub = lien.get("LienPublication")
        if lien_pub and lien_pub not in seen:
            seen.add(lien_pub)
            liens.append({"id": lien_pub, "type": lien.get("type", ""), "titre": (lien.text or "").strip()})

    return liens


def extract_references_juridiques(root: ET.Element) -> List[Dict[str, str]]:
    """Extrait les références juridiques."""
    references = []

    for ref in root.findall(".//Reference"):
        url = ref.get("URL", "")
        if url:
            titre = ref.find("Titre")
            complement = ref.find("Complement")
            references.append(
                {
                    "url": url,
                    "titre": (titre.text if titre is not None and titre.text else "").strip(),
                    "type": ref.get("type", ""),
                    "complement": (complement.text if complement is not None and complement.text else "").strip(),
                }
            )

    return references


def extract_enriched_metadata(root: ET.Element) -> Dict[str, Any]:
    """
    Extrait toutes les métadonnées enrichies d'une fiche XML Service-Public.

    Inclut :
    - voir_aussi : fiches liées (VoirAussi)
    - questions_reponses : Q&R liées
    - pour_en_savoir_plus : liens complémentaires
    - services_en_ligne : téléservices
    - qui_peut_m_aider : contacts utiles
    - abreviations : sigles et acronymes
    - situations : onglets FPE/FPT/FPH etc.
    """
    metadata = {}

    # VoirAussi - Fiches liées
    voir_aussi = []
    for va in root.findall(".//VoirAussi"):
        for fiche in va.findall(".//Fiche"):
            titre = fiche.find("Titre")
            theme = fiche.find("Theme/Titre")
            voir_aussi.append(
                {
                    "id": fiche.get("ID", ""),
                    "titre": (titre.text if titre is not None else "").strip(),
                    "theme": (theme.text if theme is not None else "").strip(),
                    "audience": fiche.get("audience", ""),
                }
            )
    if voir_aussi:
        metadata["voir_aussi"] = voir_aussi

    # QuestionReponse - Questions-réponses liées
    questions_reponses = []
    for qr in root.findall(".//QuestionReponse"):
        titre = qr.find("Titre")
        questions_reponses.append(
            {
                "id": qr.get("ID", ""),
                "titre": (titre.text if titre is not None else "").strip(),
                "audience": qr.get("audience", ""),
            }
        )
    if questions_reponses:
        metadata["questions_reponses"] = questions_reponses

    # PourEnSavoirPlus - Liens complémentaires
    pour_en_savoir_plus = []
    for pes in root.findall(".//PourEnSavoirPlus"):
        titre = pes.find("Titre")
        source = pes.find("Source")
        pour_en_savoir_plus.append(
            {
                "id": pes.get("ID", ""),
                "url": pes.get("URL", ""),
                "titre": (titre.text if titre is not None else "").strip(),
                "source": (source.text if source is not None else "").strip(),
                "type": pes.get("type", ""),
            }
        )
    if pour_en_savoir_plus:
        metadata["pour_en_savoir_plus"] = pour_en_savoir_plus

    # ServiceEnLigne - Téléservices
    services_en_ligne = []
    for sel in root.findall(".//ServiceEnLigne"):
        titre = sel.find("Titre")
        source = sel.find("Source")
        services_en_ligne.append(
            {
                "id": sel.get("ID", ""),
                "url": sel.get("URL", ""),
                "titre": (titre.text if titre is not None else "").strip(),
                "source": (source.text if source is not None else "").strip(),
                "type": sel.get("type", ""),
            }
        )
    if services_en_ligne:
        metadata["services_en_ligne"] = services_en_ligne

    # QuiPeutMAider - Contacts utiles
    qui_peut_m_aider = []
    for contact in root.findall(".//QuiPeutMAider"):
        titre = contact.find("Titre")
        qui_peut_m_aider.append(
            {
                "id": contact.get("ID", ""),
                "titre": (titre.text if titre is not None else "").strip(),
                "type": contact.get("type", ""),
            }
        )
    if qui_peut_m_aider:
        metadata["qui_peut_m_aider"] = qui_peut_m_aider

    # Abreviations - Sigles et acronymes
    abreviations = []
    for abr in root.findall(".//Abreviation"):
        titre = abr.find("Titre")
        texte = abr.find("Texte")
        texte_str = extract_text_simple(texte) if texte is not None else ""
        abreviations.append(
            {
                "id": abr.get("ID", ""),
                "sigle": (titre.text if titre is not None else "").strip(),
                "signification": texte_str.strip(),
                "type": abr.get("type", ""),
            }
        )
    if abreviations:
        metadata["abreviations"] = abreviations

    # Situations (onglets FPE/FPT/FPH)
    liste_situations = root.find(".//ListeSituations")
    if liste_situations is not None:
        situations = []
        for situation in liste_situations.findall("Situation"):
            titre = situation.find("Titre")
            if titre is not None and titre.text:
                situations.append(titre.text.strip())
        if situations:
            metadata["situations"] = situations
            metadata["affichage_situations"] = liste_situations.get("affichage", "onglet")

    return metadata


def parse_date(date_str: str) -> Optional[str]:
    """Parse une date ISO."""
    if not date_str:
        return None

    try:
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        elif "-" in date_str:
            return date_str[:10]
    except Exception:
        pass

    return date_str


# ============================================================================
# FONCTION PRINCIPALE - PARSING DOCUMENT COMPLET
# ============================================================================


def parse_fiche_xml(fiche_id: str, xml_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Parse une fiche XML depuis un fichier.

    Args:
        fiche_id: ID de la fiche (ex: "F515")
        xml_dir: Répertoire contenant les fichiers XML

    Returns:
        Dict avec doc_markdown, title, metadata, etc.
    """
    xml_file = xml_dir / f"{fiche_id}.xml"

    if not xml_file.exists():
        logger.warning("Fiche %s non trouvée : %s", fiche_id, xml_file)
        return None

    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()
    except Exception:
        logger.exception("Erreur parsing fiche %s", fiche_id)
        return None

    return _parse_xml_root(root, fiche_id)


def parse_fiche_xml_from_bytes(xml_content: bytes, fiche_id: str) -> Optional[Dict[str, Any]]:
    """
    Parse un fichier XML depuis son contenu en bytes.

    Utilisé par la page d'ingestion pour les fichiers uploadés.

    Args:
        xml_content: Contenu XML en bytes
        fiche_id: ID de la fiche (ex: "F515")

    Returns:
        Dict identique à parse_fiche_xml
    """
    try:
        root = ET.fromstring(xml_content.decode("utf-8"))
    except Exception:
        logger.exception("Erreur parsing XML %s", fiche_id)
        return None

    return _parse_xml_root(root, fiche_id)


def _parse_xml_root(root: ET.Element, fiche_id: str) -> Optional[Dict[str, Any]]:
    """
    Logique commune de parsing XML.

    Extrait depuis un Element racine déjà parsé.
    """
    # Extraire les définitions
    definitions = extract_definitions_from_root(root)
    # Métadonnées
    dc_ns = {"dc": "http://purl.org/dc/elements/1.1/"}

    title_elem = root.find(".//dc:title", dc_ns)
    title = title_elem.text if title_elem is not None and title_elem.text else fiche_id

    type_elem = root.find(".//dc:type", dc_ns)
    category = type_elem.text if type_elem is not None and type_elem.text else ""

    # Date de vérification "Vérifié le" (dc:date) - prioritaire pour le suivi de fraîcheur
    date_verif = None
    dc_date_elem = root.find(".//dc:date", dc_ns)
    if dc_date_elem is not None and dc_date_elem.text:
        dc_date_text = dc_date_elem.text.strip()
        if dc_date_text.startswith("modified "):
            date_verif = parse_date(dc_date_text.replace("modified ", ""))
        else:
            date_verif = parse_date(dc_date_text)

    # Date de modification substantielle (fallback)
    date_modif_importante = root.get("dateDerniereModificationImportante", "")
    date_modif_importante = parse_date(date_modif_importante) if date_modif_importante else None

    # Utiliser la date de vérification en priorité, sinon la date de modification importante
    date_modif = date_verif or date_modif_importante

    # Thème
    theme_elem = root.find(".//Theme/Titre")
    theme = theme_elem.text.strip() if theme_elem is not None and theme_elem.text else ""

    # ============================================================
    # CONSTRUIRE LE DOCUMENT MARKDOWN COMPLET
    # ============================================================

    doc_parts = []

    # Titre principal (H1)
    doc_parts.append(f"# {title}")

    # Avertissement/Chapeau (élément important en début de document)
    avertissement_text = ""
    avertissement_elem = root.find("./Avertissement")
    if avertissement_elem is not None:
        avert_titre_elem = avertissement_elem.find("Titre")
        avert_texte_elem = avertissement_elem.find("Texte")

        avert_titre = extract_text_simple(avert_titre_elem) if avert_titre_elem is not None else "Avertissement"

        if avert_texte_elem is not None:
            avert_md = extract_text_markdown(avert_texte_elem, definitions=definitions)
            avert_md = clean_markdown(avert_md)
            if avert_md:
                doc_parts.append(f"\n> ⚠️ **{avert_titre}**\n>\n> {avert_md.replace(chr(10), chr(10) + '> ')}")
            # Stocker en texte brut pour metadata
            avertissement_text = extract_text_simple(avert_texte_elem)

    # Introduction (extraire et stocker dans metadata)
    introduction_text = ""
    introduction_parts = []
    introduction_plain_parts = []
    for introduction_elem in root.findall(".//Introduction/Texte"):
        intro_text = extract_text_markdown(introduction_elem, definitions=definitions)
        intro_text = clean_markdown(intro_text)
        if intro_text:
            introduction_parts.append(intro_text)

        intro_plain_text = extract_text_simple(introduction_elem).strip()
        if intro_plain_text:
            introduction_plain_parts.append(intro_plain_text)

    if introduction_parts:
        doc_parts.append("\n\n".join(introduction_parts))
        # Stocker aussi en texte brut pour metadata (sans Markdown)
        introduction_text = "\n\n".join(introduction_plain_parts)

    # Vérifier s'il y a des situations (onglets FPE/FPT/FPH)
    liste_situations = root.find(".//ListeSituations")

    if liste_situations is not None:
        # Certaines fiches avec situations ont un bloc de texte général entre
        # l'introduction XML et les onglets. Il doit rester dans le préambule.
        for child in root:
            if child is liste_situations:
                break
            if child.tag != "Texte":
                continue

            preamble_text = extract_text_markdown(child, definitions=definitions)
            preamble_text = clean_markdown(preamble_text)
            if preamble_text:
                doc_parts.append(preamble_text)

            preamble_plain_text = extract_text_simple(child).strip()
            if preamble_plain_text:
                introduction_plain_parts.append(preamble_plain_text)
                introduction_text = "\n\n".join(introduction_plain_parts)

        # Fiche avec situations multiples
        situations = liste_situations.findall("Situation")

        for situation in situations:
            situation_titre_elem = situation.find("Titre")
            situation_titre = situation_titre_elem.text if situation_titre_elem is not None else None

            if situation_titre:
                doc_parts.append(f"\n## {situation_titre}")

            texte_situation = situation.find("Texte")

            if texte_situation is not None:
                # Traiter les chapitres
                for chapitre in texte_situation.findall("Chapitre"):
                    chapitre_md = _extract_chapitre_markdown(chapitre, definitions)
                    if chapitre_md:
                        doc_parts.append(chapitre_md)

                # S'il n'y a pas de chapitres, extraire le texte direct
                if not texte_situation.findall("Chapitre"):
                    texte = extract_text_markdown(texte_situation, definitions=definitions)
                    texte = clean_markdown(texte)
                    if texte:
                        doc_parts.append(texte)
    else:
        # Fiche sans situations
        texte_elem = None
        for child in root:
            if child.tag == "Texte":
                texte_elem = child
                break

        if texte_elem is not None:
            # Traiter les chapitres
            for chapitre in texte_elem.findall("Chapitre"):
                chapitre_md = _extract_chapitre_markdown(chapitre, definitions)
                if chapitre_md:
                    doc_parts.append(chapitre_md)

            # S'il n'y a pas de chapitres, extraire le texte direct
            if not texte_elem.findall("Chapitre"):
                texte = extract_text_markdown(texte_elem, definitions=definitions)
                texte = clean_markdown(texte)
                if texte:
                    doc_parts.append(texte)

    # Assembler le document
    doc_markdown = "\n\n".join(doc_parts)
    doc_markdown = clean_markdown(doc_markdown)

    # ============================================================
    # MÉTADONNÉES COMPLÈTES
    # ============================================================

    # Extraire les métadonnées enrichies (liens, contacts, abréviations...)
    enriched = extract_enriched_metadata(root)

    metadata = {
        "parser_version": XML_PARSER_VERSION,
        "source": "service_public",
        "category": category,  # "Fiche pratique" ou "Question-réponse"
        "theme": theme,  # "Travail - Formation" etc.
        "avertissement": avertissement_text,  # Texte chapeau/avertissement important
        "introduction": introduction_text,  # Texte brut de l'introduction
        "subtitles": extract_subtitles(root),  # Breadcrumb court
        "context": extract_fil_ariane(root),  # Breadcrumb complet
        "liens_internes": extract_liens_internes(root),
        "references_juridiques": extract_references_juridiques(root),
        "definitions": [{"id": k, "texte": v} for k, v in definitions.items()],
        # Dates
        "date_verification": date_verif,  # "Vérifié le" - date de vérification
        "date_modif_importante": date_modif_importante,  # Date de modification substantielle
        # Métadonnées enrichies
        **enriched,  # voir_aussi, questions_reponses, services_en_ligne, etc.
    }

    return {
        "doc_markdown": doc_markdown,
        "title": title,
        "short_id": fiche_id,
        "source": "service_public",
        "source_url": root.get("spUrl", f"https://www.service-public.fr/particuliers/vosdroits/{fiche_id}"),
        "last_updated_date": date_modif,
        "token_count": count_tokens(doc_markdown),
        "char_count": len(doc_markdown),
        "doc_text_hash": compute_text_hash(doc_markdown),
        "metadata": metadata,
    }


def _extract_chapitre_markdown(chapitre: ET.Element, definitions: Dict[str, str] | None = None) -> str:
    """Extrait un chapitre en Markdown avec hiérarchie ## / ###."""
    parts = []

    # Titre du chapitre (H2)
    titre_elem = chapitre.find("Titre")
    if titre_elem is not None:
        titre = extract_text_simple(titre_elem)
        if titre:
            parts.append(f"\n## {titre}")

    # SousChapitres
    sous_chapitres = chapitre.findall("SousChapitre")

    if sous_chapitres:
        # D'abord le contenu avant les sous-chapitres
        for elem in chapitre:
            if elem.tag == "Titre":
                continue
            if elem.tag == "SousChapitre":
                break
            elem_text = extract_text_markdown(elem, level=0, definitions=definitions)
            if elem_text:
                parts.append(elem_text.strip())

        # Puis chaque sous-chapitre
        for sc in sous_chapitres:
            sc_titre_elem = sc.find("Titre")
            sc_titre = extract_text_simple(sc_titre_elem) if sc_titre_elem is not None else None

            if sc_titre:
                parts.append(f"\n### {sc_titre}")

            for elem in sc:
                if elem.tag == "Titre":
                    continue
                elem_text = extract_text_markdown(elem, level=1, definitions=definitions)
                if elem_text:
                    parts.append(elem_text.strip())
    else:
        # Pas de sous-chapitres
        for elem in chapitre:
            if elem.tag != "Titre":
                elem_text = extract_text_markdown(elem, level=0, definitions=definitions)
                if elem_text:
                    parts.append(elem_text.strip())

    return "\n\n".join(parts)


# ============================================================================
# FONCTION BATCH
# ============================================================================


def parse_multiple_fiches(fiche_ids: List[str], xml_dir: Path) -> List[Dict[str, Any]]:
    """Parse plusieurs fiches."""
    results = []

    for fiche_id in fiche_ids:
        result = parse_fiche_xml(fiche_id, xml_dir)
        if result:
            logger.info("%s: %s... (%s tokens)", fiche_id, result["title"][:50], result["token_count"])
            results.append(result)
        else:
            logger.warning("%s: échec du parsing", fiche_id)

    return results


# ============================================================================
# TEST
# ============================================================================

if __name__ == "__main__":
    from pathlib import Path

    # Test sur une fiche
    xml_dir = Path("data/vosdroits-latest")

    if not xml_dir.exists():
        print("Répertoire XML non trouvé")
    else:
        result = parse_fiche_xml("F515", xml_dir)

        if result:
            print("\n" + "=" * 60)
            print(f"Titre: {result['title']}")
            print(f"ID: {result['short_id']}")
            print(f"Tokens: {result['token_count']}")
            print(f"Chars: {result['char_count']}")
            print("=" * 60)
            print("\nMarkdown (500 premiers chars):")
            print(result["doc_markdown"][:500])
