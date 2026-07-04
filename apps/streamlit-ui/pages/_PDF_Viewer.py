"""
PDF Viewer - Affiche un PDF en plein écran

Chaîne de résolution pour rag_doc_id :
1. legacy_doc_id → lit les bytes depuis la table `documents`
2. storage_path  → lit le fichier depuis la dropzone S3 (corpus PDF
   ministériels: MI/MASA/MATTE/MSO, storage_path = cle_bucket du manifest
   Grist; les originaux bureautiques .doc/.xlsx sont proposés au
   téléchargement)
3. source_url    → redirige vers l'URL externe (service-public.fr, legifrance.gouv.fr)

URLs supportées :
- /PDF_Viewer?rag_doc_id=<uuid> → Résolution via rag_documents (legacy DB > URL)
- /PDF_Viewer?doc_id=<uuid>     → Legacy direct: lit depuis table documents (bytes en DB)
"""

import streamlit as st

# Config minimaliste (AVANT tout autre import Streamlit)
st.set_page_config(
    page_title="PDF",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={"Get Help": None, "Report a bug": None, "About": None},
)

# CSS minimal pour plein écran
st.markdown(
    """<style>
#MainMenu,footer,header,.stDeployButton,[data-testid="stSidebar"]{display:none!important}
.main .block-container,.main,.element-container,.stApp{padding:0!important;margin:0!important;max-width:100%!important}
iframe{border:none!important;width:100vw!important;height:100vh!important;position:fixed!important;top:0!important;left:0!important}
</style>""",
    unsafe_allow_html=True,
)

# Imports (après le CSS pour un rendu initial plus rapide)
import base64
from typing import Optional

import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════════
# Database connection
# ═══════════════════════════════════════════════════════════════════════════════

from src.ui.db_utils import get_engine


def get_rag_doc_info(rag_doc_id: str) -> dict | None:
    """
    Récupère les infos d'un rag_document (legacy_doc_id, title, source_url).

    Returns:
        dict with keys {legacy_doc_id, title, source_url} or None
    """
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT title, source_url, legacy_doc_id, storage_path
                FROM rag_documents
                WHERE doc_id = :doc_id
            """),
            {"doc_id": rag_doc_id},
        )
        row = result.fetchone()

        if not row:
            return None

        return {
            "title": row[0] or "Document",
            "source_url": row[1],
            "legacy_doc_id": str(row[2]) if row[2] else None,
            "storage_path": str(row[3]).strip() if row[3] else None,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy: Load PDF from documents table
# ═══════════════════════════════════════════════════════════════════════════════


def get_document_pdf_legacy(doc_id: str) -> Optional[dict]:
    """
    Récupère un PDF depuis l'ancienne table documents (bytes en DB).

    Args:
        doc_id: UUID du document

    Returns:
        dict avec 'filename' et 'data' (bytes), ou None
    """
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT filename, data
                FROM documents
                WHERE id = :doc_id
            """),
            {"doc_id": doc_id},
        )
        row = result.fetchone()

        if row:
            return {"filename": row[0], "data": row[1]}
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# PDF Display
# ═══════════════════════════════════════════════════════════════════════════════


def display_pdf_from_bytes(pdf_bytes: bytes, filename: str = "document.pdf"):
    """
    Affiche un PDF depuis des bytes (méthode legacy avec PDF.js).
    """
    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

    # Viewer PDF.js complet
    pdf_viewer_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{filename}</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: Arial, sans-serif;
                background: #525659;
                overflow: hidden;
            }}
            #viewer-container {{
                width: 100vw;
                height: 100vh;
                overflow: auto;
                padding: 20px;
            }}
            #pdf-pages {{
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 20px;
            }}
            .page-container {{
                position: relative;
                background: white;
                box-shadow: 0 4px 20px rgba(0,0,0,0.15);
                border: 1px solid rgba(0,0,0,0.1);
            }}
            canvas {{
                display: block;
                image-rendering: -webkit-optimize-contrast;
                image-rendering: crisp-edges;
            }}
            .textLayer {{
                position: absolute;
                left: 0; top: 0; right: 0; bottom: 0;
                overflow: hidden;
                opacity: 0.2;
                line-height: 1.0;
            }}
            .textLayer > span {{
                color: transparent;
                position: absolute;
                white-space: pre;
                cursor: text;
                transform-origin: 0% 0%;
            }}
            .textLayer ::selection {{ background: rgba(0, 0, 255, 0.3); }}
            #loading {{
                color: white;
                text-align: center;
                padding: 50px;
                font-size: 18px;
            }}
            .controls {{
                position: fixed;
                top: 10px;
                right: 10px;
                background: rgba(0,0,0,0.8);
                padding: 10px;
                border-radius: 5px;
                z-index: 1000;
                display: flex;
                gap: 8px;
            }}
            .controls button {{
                background: #4CAF50;
                color: white;
                border: none;
                padding: 8px 12px;
                border-radius: 3px;
                cursor: pointer;
                font-size: 13px;
            }}
            .controls button:hover {{ background: #45a049; }}
        </style>
    </head>
    <body>
        <div class="controls">
            <button onclick="zoomIn()">🔍 +</button>
            <button onclick="zoomOut()">🔍 −</button>
            <button onclick="resetZoom()">↺ Reset</button>
            <button onclick="downloadPDF()">⬇️ Download</button>
        </div>
        <div id="viewer-container">
            <div id="loading">📄 Loading PDF...</div>
            <div id="pdf-pages"></div>
        </div>
        
        <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
        <script>
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            
            const pdfData = atob('{pdf_base64}');
            const pdfArray = new Uint8Array(pdfData.length);
            for (let i = 0; i < pdfData.length; i++) {{
                pdfArray[i] = pdfData.charCodeAt(i);
            }}
            
            let pdfDoc = null;
            let currentScale = 1.8;
            const pixelRatio = window.devicePixelRatio || 1;
            
            async function renderAllPages() {{
                const container = document.getElementById('pdf-pages');
                container.innerHTML = '';
                for (let pageNum = 1; pageNum <= pdfDoc.numPages; pageNum++) {{
                    await renderPage(pageNum, container);
                }}
            }}
            
            async function renderPage(pageNum, container) {{
                const page = await pdfDoc.getPage(pageNum);
                const viewport = page.getViewport({{ scale: currentScale }});
                
                const pageContainer = document.createElement('div');
                pageContainer.className = 'page-container';
                pageContainer.style.width = viewport.width + 'px';
                pageContainer.style.height = viewport.height + 'px';
                
                const canvas = document.createElement('canvas');
                const context = canvas.getContext('2d');
                
                canvas.width = viewport.width * pixelRatio;
                canvas.height = viewport.height * pixelRatio;
                canvas.style.width = viewport.width + 'px';
                canvas.style.height = viewport.height + 'px';
                context.scale(pixelRatio, pixelRatio);
                
                const textLayerDiv = document.createElement('div');
                textLayerDiv.className = 'textLayer';
                textLayerDiv.style.width = viewport.width + 'px';
                textLayerDiv.style.height = viewport.height + 'px';
                
                pageContainer.appendChild(canvas);
                pageContainer.appendChild(textLayerDiv);
                container.appendChild(pageContainer);
                
                context.imageSmoothingEnabled = true;
                context.imageSmoothingQuality = 'high';
                
                await page.render({{
                    canvasContext: context,
                    viewport: viewport,
                    intent: 'display',
                    enableWebGL: false,
                    renderInteractiveForms: false
                }}).promise;
                
                const textContent = await page.getTextContent();
                pdfjsLib.renderTextLayer({{
                    textContent: textContent,
                    container: textLayerDiv,
                    viewport: viewport,
                    textDivs: []
                }});
            }}
            
            pdfjsLib.getDocument({{ data: pdfArray }}).promise.then(function(pdf) {{
                pdfDoc = pdf;
                document.getElementById('loading').style.display = 'none';
                renderAllPages();
            }}).catch(function(error) {{
                console.error('Error loading PDF:', error);
                document.getElementById('loading').innerHTML = '❌ Failed to load PDF';
            }});
            
            function zoomIn() {{ currentScale += 0.25; renderAllPages(); }}
            function zoomOut() {{ if (currentScale > 0.5) {{ currentScale -= 0.25; renderAllPages(); }} }}
            function resetZoom() {{ currentScale = 1.5; renderAllPages(); }}
            function downloadPDF() {{
                const blob = new Blob([pdfArray], {{ type: 'application/pdf' }});
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = '{filename}';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }}
        </script>
    </body>
    </html>
    """

    components.html(pdf_viewer_html, height=900, scrolling=False)


def display_dropzone_document(storage_path: str, title: str) -> bool:
    """Affiche un document des corpus PDF ministériels depuis la dropzone S3.

    storage_path = cle_bucket du manifest Grist (ex: mi/{uid}_{nom}.pdf).
    Les PDF sont rendus dans le viewer; les originaux bureautiques
    (.doc/.docx/.xls/.xlsx, convertis en PDF seulement dans le pipeline)
    sont proposés au téléchargement. Retourne False si la lecture échoue.
    """
    from src.ui.source_import import DropzoneUploader, SourceImportError, content_type_for

    try:
        dropzone = DropzoneUploader.from_env()
        data = dropzone.fetch_file(storage_path)
    except SourceImportError:
        return False

    filename = storage_path.rsplit("/", 1)[-1] or "document.pdf"
    if filename.lower().endswith(".pdf"):
        display_pdf_from_bytes(data, filename)
        return True

    st.markdown(
        f"""
    <div style="display: flex; justify-content: center; align-items: center; height: 60vh; flex-direction: column;">
        <h2>📄 {title}</h2>
        <p style="font-size: 16px; margin: 12px 0;">Document source au format bureautique ({filename.rsplit(".", 1)[-1]}) — téléchargez-le pour l'ouvrir.</p>
    </div>
    """,
        unsafe_allow_html=True,
    )
    _, center, _ = st.columns([2, 1, 2])
    with center:
        st.download_button(
            f"⬇️ Télécharger {filename}",
            data=data,
            file_name=filename,
            mime=content_type_for(filename),
            use_container_width=True,
        )
    return True


def display_error(message: str, help_text: str = ""):
    """Affiche une page d'erreur stylée."""
    st.markdown(
        f"""
    <div style="display: flex; justify-content: center; align-items: center; height: 100vh; flex-direction: column;">
        <h1 style="color: #e74c3c;">❌ {message}</h1>
        {f'<p style="font-size: 18px; margin: 16px 0;">{help_text}</p>' if help_text else ""}
        <button onclick="location.reload()" style="margin-top: 16px; padding: 8px 16px; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer;">🔄 Réessayer</button>
    </div>
    """,
        unsafe_allow_html=True,
    )


def display_placeholder():
    """Affiche la page d'accueil quand aucun document n'est spécifié."""
    st.markdown(
        """
    <div style="display: flex; justify-content: center; align-items: center; height: 100vh; flex-direction: column;">
        <h1>📄 PDF Viewer</h1>
        <p>Aucun document spécifié dans l'URL.</p>
    </div>
    """,
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

query_params = st.query_params

rag_doc_id = query_params.get("rag_doc_id")
doc_id = query_params.get("doc_id")

if not rag_doc_id and not doc_id:
    display_placeholder()

elif rag_doc_id:
    try:
        info = get_rag_doc_info(rag_doc_id)

        if not info:
            display_error("Document introuvable", f"Le document {rag_doc_id[:8]}... n'existe pas dans rag_documents.")
        else:
            title = info["title"]
            pdf_loaded = False

            if info["legacy_doc_id"]:
                doc = get_document_pdf_legacy(info["legacy_doc_id"])
                if doc:
                    display_pdf_from_bytes(bytes(doc["data"]), doc["filename"])
                    pdf_loaded = True

            if not pdf_loaded and info.get("storage_path"):
                pdf_loaded = display_dropzone_document(info["storage_path"], title)

            if not pdf_loaded and info["source_url"] and info["source_url"].startswith("http"):
                st.markdown(
                    f"""
                <script>window.open("{info["source_url"]}", "_blank");</script>
                <div style="text-align: center; padding: 50px;">
                    <h2>📄 {title}</h2>
                    <p><a href="{info["source_url"]}" target="_blank">Cliquez ici pour ouvrir le document</a></p>
                </div>
                """,
                    unsafe_allow_html=True,
                )
                pdf_loaded = True

            if not pdf_loaded:
                display_error("Aucune source disponible", f"Ni PDF en base, ni URL pour « {title} ».")

    except Exception as e:
        display_error("Erreur de chargement", str(e))

elif doc_id:
    try:
        doc = get_document_pdf_legacy(doc_id)

        if not doc:
            display_error("Document introuvable", f"Le document {doc_id[:8]}... n'existe pas dans la table documents.")
        else:
            display_pdf_from_bytes(bytes(doc["data"]), doc["filename"])

    except Exception as e:
        error_msg = str(e)

        if "timeout" in error_msg.lower():
            display_error("Timeout", "La base de données met trop de temps à répondre.")
        elif "connection" in error_msg.lower():
            display_error("Connexion impossible", "Problème de connexion à la base de données.")
        else:
            display_error("Erreur inattendue", error_msg)
