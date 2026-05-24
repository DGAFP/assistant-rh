"""
Helper pour générer les URLs de documents de manière cohérente
dans toute l'application Streamlit.

Supporte deux types de documents :
1. RAG Documents (rag_documents) → storage_path S3 → presigned URL
2. Legacy Documents (documents) → bytes en DB
"""
import os
from typing import Optional

import streamlit as st


def get_document_url(
    doc_id: str, 
    relative: bool = True, 
    pdf_only: bool = True,
    is_rag_doc: bool = False,
) -> str:
    """
    Génère l'URL pour accéder à un document spécifique.
    
    Cette fonction s'adapte automatiquement à l'environnement (dev/prod)
    et au type de document (RAG avec S3 ou legacy avec bytes en DB).
    
    Args:
        doc_id: UUID du document (string)
        relative: Si True (défaut), retourne une URL relative.
                 Si False, retourne une URL absolue.
        pdf_only: Si True (défaut), utilise PDF_Viewer (juste le PDF, sans navigation).
                 Si False, utilise Document_Viewer (page complète avec chunks).
        is_rag_doc: Si True, utilise rag_doc_id (pour rag_documents avec S3).
                   Si False (défaut), utilise doc_id (legacy documents table).
    
    Returns:
        URL du document (relative ou absolue selon le paramètre)
    
    Examples:
        >>> # RAG Document (S3 via rag_documents) - NOUVEAU
        >>> url = get_document_url("abc123...", is_rag_doc=True)
        >>> # → "/PDF_Viewer?rag_doc_id=abc123..."
        
        >>> # Legacy Document (bytes en DB)
        >>> url = get_document_url("abc123...")
        >>> # → "/PDF_Viewer?doc_id=abc123..."
        
        >>> # URL absolue (pour emails, exports, etc.)
        >>> url = get_document_url("abc123...", relative=False, is_rag_doc=True)
        >>> # Dev:  "http://localhost:8501/PDF_Viewer?rag_doc_id=abc123..."
        >>> # Prod: "https://votre-app.scalingo.io/PDF_Viewer?rag_doc_id=abc123..."
    
    Notes:
        - Les URLs relatives fonctionnent automatiquement en dev et prod
        - is_rag_doc=True utilise les presigned URLs S3 (plus rapide)
        - PDF_Viewer s'ouvre idéalement dans un nouvel onglet (target="_blank")
    """
    if not doc_id:
        raise ValueError("doc_id ne peut pas être vide")
    
    # Choisir la page selon le mode
    page = "PDF_Viewer" if pdf_only else "Document_Viewer"
    
    # Choisir le paramètre selon le type de document
    param = "rag_doc_id" if is_rag_doc else "doc_id"
    
    # URL relative (recommandée - fonctionne partout)
    if relative:
        return f"/{page}?{param}={doc_id}"
    
    # URL absolue (pour partage externe)
    return _get_absolute_url(doc_id, page, param)


def get_rag_document_url(doc_id: str, relative: bool = True) -> str:
    """
    Raccourci pour générer l'URL d'un document RAG (avec S3).
    
    Args:
        doc_id: UUID du document dans rag_documents
        relative: Si True (défaut), URL relative
    
    Returns:
        URL vers le PDF Viewer avec presigned URL S3
    
    Example:
        >>> url = get_rag_document_url("abc123...")
        >>> # → "/PDF_Viewer?rag_doc_id=abc123..."
    """
    return get_document_url(doc_id, relative=relative, pdf_only=True, is_rag_doc=True)


def get_direct_pdf_url(doc_id: str) -> Optional[str]:  # noqa: ARG001
    """S3 has been removed. Always returns None (kept for backward compatibility)."""
    return None



def _get_absolute_url(doc_id: str, page: str = "PDF_Viewer", param: str = "doc_id") -> str:
    """
    Génère une URL absolue en détectant automatiquement l'environnement.
    
    Args:
        doc_id: UUID du document
        page: Nom de la page (PDF_Viewer ou Document_Viewer)
        param: Nom du paramètre URL (doc_id ou rag_doc_id)
    
    Ordre de priorité :
    1. Variable d'environnement STREAMLIT_SERVER_URL
    2. Détection via st.get_option (Streamlit Cloud, Scalingo, etc.)
    3. Fallback sur localhost (développement local)
    """
    # 1. Variable d'environnement (la plus fiable)
    server_url = os.environ.get("STREAMLIT_SERVER_URL")
    if server_url:
        server_url = server_url.rstrip("/")
        return f"{server_url}/{page}?{param}={doc_id}"
    
    # 2. Détection via Streamlit (si disponible)
    try:
        if hasattr(st, 'get_option'):
            server_address = st.get_option("browser.serverAddress")
            server_port = st.get_option("browser.serverPort")
            
            # Si on a une vraie adresse (pas localhost)
            if server_address and server_address != "localhost":
                protocol = "https" if server_port in [443, 80] else "https"
                return f"{protocol}://{server_address}/{page}?{param}={doc_id}"
            
            # Localhost avec port
            if server_address == "localhost" and server_port:
                return f"http://localhost:{server_port}/{page}?{param}={doc_id}"
    except Exception:
        pass
    
    # 3. Fallback : localhost:8501 (développement local)
    return f"http://localhost:8501/{page}?{param}={doc_id}"


def get_markdown_link(
    doc_id: str, 
    text: str = "📄 Voir le document source", 
    relative: bool = True,
    new_tab: bool = True,
    pdf_only: bool = True
) -> str:
    """
    Génère un lien Markdown ou HTML prêt à l'emploi.
    
    Args:
        doc_id: UUID du document
        text: Texte du lien
        relative: Si True, utilise une URL relative
        new_tab: Si True, ouvre dans un nouvel onglet (génère du HTML)
        pdf_only: Si True, affiche juste le PDF. Si False, affiche la page complète.
    
    Returns:
        String Markdown ou HTML formaté
    
    Example:
        >>> # Lien qui s'ouvre dans un nouvel onglet (recommandé)
        >>> link = get_markdown_link("abc123...", "Voir le PDF", new_tab=True)
        >>> st.markdown(link, unsafe_allow_html=True)
        
        >>> # Lien Markdown simple (même onglet)
        >>> link = get_markdown_link("abc123...", "Voir le PDF", new_tab=False)
        >>> st.markdown(link)
    """
    url = get_document_url(doc_id, relative=relative, pdf_only=pdf_only)
    
    if new_tab:
        # HTML avec target="_blank" pour ouvrir dans un nouvel onglet
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{text}</a>'
    else:
        # Markdown simple
        return f"[{text}]({url})"


def display_document_link(
    doc_id: str, 
    text: str = "📄 Voir le document source",
    new_tab: bool = True,
    pdf_only: bool = True,
    **kwargs
):
    """
    Affiche directement un lien vers un document dans Streamlit.
    
    Args:
        doc_id: UUID du document
        text: Texte du lien
        new_tab: Si True (défaut), ouvre dans un nouvel onglet
        pdf_only: Si True (défaut), affiche juste le PDF
        **kwargs: Arguments supplémentaires pour st.markdown()
    
    Example:
        >>> # S'ouvre dans un nouvel onglet (recommandé)
        >>> display_document_link("abc123...", "Voir le PDF source")
        
        >>> # S'ouvre dans le même onglet
        >>> display_document_link("abc123...", "Voir le PDF", new_tab=False)
        
        >>> # Page complète avec chunks
        >>> display_document_link("abc123...", "Voir détails", pdf_only=False)
    """
    link = get_markdown_link(doc_id, text, relative=True, new_tab=new_tab, pdf_only=pdf_only)
    
    if new_tab:
        st.markdown(link, unsafe_allow_html=True, **kwargs)
    else:
        st.markdown(link, **kwargs)


def chunk_has_source(chunk: dict) -> bool:
    """
    Vérifie si un chunk a un document source lié.
    
    Args:
        chunk: Dictionnaire représentant un chunk
    
    Returns:
        True si le chunk a un source_document_id valide
    
    Example:
        >>> if chunk_has_source(chunk):
        >>>     display_document_link(chunk['source_document_id'])
    """
    return 'source_document_id' in chunk and chunk['source_document_id'] is not None


def display_chunks_with_sources(chunks: list[dict], new_tab: bool = True, pdf_only: bool = True):
    """
    Affiche une liste de chunks avec liens vers leurs documents sources.
    
    Args:
        chunks: Liste de dictionnaires représentant des chunks.
                Chaque chunk doit avoir 'chunk_text' et optionnellement 'source_document_id'.
        new_tab: Si True (défaut), les liens s'ouvrent dans un nouvel onglet
        pdf_only: Si True (défaut), affiche juste le PDF. Si False, affiche la page complète.
    
    Example:
        >>> chunks = retriever.get_relevant_chunks(question)
        >>> # PDF dans un nouvel onglet (recommandé)
        >>> display_chunks_with_sources(chunks)
        
        >>> # Page complète dans le même onglet
        >>> display_chunks_with_sources(chunks, new_tab=False, pdf_only=False)
    """
    for i, chunk in enumerate(chunks, 1):
        with st.container(border=True):
            st.markdown(f"**Chunk {i}**")
            st.markdown(chunk.get('chunk_text', ''))
            
            if chunk_has_source(chunk):
                display_document_link(
                    chunk['source_document_id'],
                    text="📄 Voir le document source",
                    new_tab=new_tab,
                    pdf_only=pdf_only
                )
            else:
                st.caption("⚠️ Source du document non disponible")


# ============================================================================
# Configuration
# ============================================================================

def configure_production_url(url: str):
    """
    Configure l'URL de production (à appeler au démarrage de l'app).
    
    Args:
        url: URL complète de l'app en production (ex: "https://mon-app.scalingo.io")
    
    Example:
        >>> # Dans Home.py ou au début de l'app
        >>> if os.environ.get("ENV") == "production":
        >>>     configure_production_url("https://assistant-rh.scalingo.io")
    """
    os.environ["STREAMLIT_SERVER_URL"] = url.rstrip("/")


def get_current_base_url() -> str:
    """
    Retourne l'URL de base de l'application (pour debug).
    
    Returns:
        URL de base (ex: "http://localhost:8501" ou "https://mon-app.scalingo.io")
    
    Example:
        >>> print(f"App running at: {get_current_base_url()}")
    """
    # Essayer de récupérer depuis l'environnement
    server_url = os.environ.get("STREAMLIT_SERVER_URL")
    if server_url:
        return server_url
    
    # Sinon, détecter via Streamlit
    try:
        if hasattr(st, 'get_option'):
            server_address = st.get_option("browser.serverAddress")
            server_port = st.get_option("browser.serverPort")
            
            if server_address and server_address != "localhost":
                return f"https://{server_address}"
            
            if server_address == "localhost" and server_port:
                return f"http://localhost:{server_port}"
    except Exception:
        pass
    
    return "http://localhost:8501"


# ============================================================================
# Tests (pour vérification)
# ============================================================================

if __name__ == "__main__":
    # Tests basiques
    test_uuid = "abc12345-1234-1234-1234-123456789abc"
    
    print("🧪 Tests du module document_url_helper")
    print("=" * 60)
    print()
    
    print("1. URL relative (recommandée):")
    print(f"   {get_document_url(test_uuid, relative=True)}")
    print()
    
    print("2. URL absolue (auto-détection):")
    print(f"   {get_document_url(test_uuid, relative=False)}")
    print()
    
    print("3. Lien Markdown:")
    print(f"   {get_markdown_link(test_uuid, 'Voir le document')}")
    print()
    
    print("4. URL de base actuelle:")
    print(f"   {get_current_base_url()}")
    print()
    
    print("5. Simulation prod (avec STREAMLIT_SERVER_URL):")
    configure_production_url("https://mon-app.scalingo.io")
    print(f"   {get_document_url(test_uuid, relative=False)}")
    print()
    
    print("✅ Tests terminés")

