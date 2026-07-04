"""Page admin d'import de sources (issue #249).

Point d'entrée unique du corpus: un PDF déposé ici part dans la dropzone S3
ET sa ligne Grist est créée/complétée dans la même action — le bucket et le
manifest ne peuvent pas dériver. Les ajouts Légifrance/Service-public créent
une ligne du référentiel consommée par les pipelines existants.
"""

import warnings

warnings.filterwarnings("ignore", message=".*st.cache.*")

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Import de sources", page_icon="📤", layout="wide")

from src.ui.admin_auth import require_admin, show_admin_badge

require_admin()
show_admin_badge()

from assistant_rh_data_engineering.utils.grist import GristClient, GristError

from src.ui.source_import import (
    PDF_CORPORA,
    DropzoneUploader,
    SourceImportError,
    build_cle_bucket,
    build_new_pdf_row,
    build_text_id_row,
    build_uid_from_bytes,
    find_row_by_uid,
    rows_missing_cle_bucket,
)

st.title("📤 Import de sources documentaires")
st.caption(
    "Dépose le PDF dans la dropzone Scaleway et met à jour le référentiel Grist "
    "dans la même action. Les documents sont ingérés au prochain run planifié."
)


@st.cache_resource
def _grist_client() -> GristClient:
    return GristClient()


def _load_records() -> list[dict]:
    return _grist_client().list_records()


def _row_label(record: dict) -> str:
    fields = record.get("fields") or {}
    titre = str(fields.get("titre_document") or "(sans titre)")
    uid = str(fields.get("uid") or "?")
    return f"{titre[:80]} — uid {uid}"


tab_pdf, tab_texte = st.tabs(["📄 PDF ministériel", "⚖️ Texte Légifrance / Service-public"])

with tab_pdf:
    corpus = st.selectbox("Corpus", PDF_CORPORA, key="pdf_corpus")

    try:
        records = _load_records()
    except GristError as exc:
        st.error(f"Grist inaccessible: {exc}")
        st.stop()

    pending_rows = rows_missing_cle_bucket(records, corpus)
    mode_attach = f"Compléter une ligne existante sans PDF ({len(pending_rows)} en attente)"
    mode_new = "Nouveau document"
    mode = st.radio("Mode", [mode_attach, mode_new], key="pdf_mode", horizontal=True)

    selected_row = None
    titre = ""
    sous_thematique = ""
    date_publication = None

    if mode == mode_attach:
        if not pending_rows:
            st.info(f"Aucune ligne {corpus} en attente de PDF — utiliser « Nouveau document ».")
        else:
            selected_row = st.selectbox(
                "Document du référentiel (sans cle_bucket)",
                pending_rows,
                format_func=_row_label,
                key="pdf_row",
            )
    else:
        titre = st.text_input("Titre du document", key="pdf_titre")
        sous_thematique = st.text_input("Sous-thématique (optionnel)", key="pdf_sous_them")
        date_value = st.date_input("Date de publication (optionnel)", value=None, key="pdf_date")
        date_publication = date_value.isoformat() if date_value else None

    uploaded = st.file_uploader("Fichier PDF", type=["pdf"], key="pdf_file")

    if st.button("Importer le PDF", type="primary", disabled=uploaded is None, key="pdf_submit"):
        try:
            pdf_bytes = uploaded.getvalue()
            if not pdf_bytes:
                raise SourceImportError("Fichier vide.")
            content_uid = build_uid_from_bytes(pdf_bytes)

            duplicate = find_row_by_uid(records, content_uid)
            if duplicate and (mode == mode_new):
                raise SourceImportError(f"Ce PDF existe déjà dans le référentiel: {_row_label(duplicate)}")

            if mode == mode_attach:
                if selected_row is None:
                    raise SourceImportError("Sélectionner une ligne du référentiel.")
                row_fields = selected_row.get("fields") or {}
                row_uid = str(row_fields.get("uid") or "").strip() or content_uid
                cle_bucket = build_cle_bucket(corpus, row_uid, uploaded.name)
            else:
                cle_bucket = build_cle_bucket(corpus, content_uid, uploaded.name)

            uploader = DropzoneUploader.from_env()
            uri = uploader.upload_pdf(cle_bucket, pdf_bytes)

            client = _grist_client()
            if mode == mode_attach:
                client.writeback_status(int(selected_row["id"]), {"cle_bucket": cle_bucket})
                st.success(f"PDF déposé ({uri}) et ligne complétée: {_row_label(selected_row)}")
            else:
                fields = build_new_pdf_row(
                    corpus=corpus,
                    uid=content_uid,
                    titre=titre,
                    cle_bucket=cle_bucket,
                    sous_thematique=sous_thematique,
                    date_publication=date_publication,
                )
                created = client.add_records([{"fields": fields}])
                st.success(f"PDF déposé ({uri}) — ligne Grist créée (record {created[0]}).")
            st.info("Le document sera ingéré au prochain run planifié du pipeline.")
        except (SourceImportError, GristError) as exc:
            st.error(str(exc))

with tab_texte:
    st.caption(
        "Ajout d'un texte par identifiant — Légifrance (LEGIARTI/LEGITEXT/JORFTEXT) "
        "ou fiche Service-public (Fxxxxx). Pris en compte par les pipelines existants."
    )
    text_id = st.text_input("Identifiant", key="texte_id", placeholder="LEGIARTI000006900846 ou F12345")
    titre_texte = st.text_input("Titre du texte", key="texte_titre")
    sous_them_texte = st.text_input("Sous-thématique (optionnel)", key="texte_sous_them")

    if st.button("Ajouter au référentiel", type="primary", disabled=not text_id.strip(), key="texte_submit"):
        try:
            fields = build_text_id_row(
                text_id=text_id,
                titre=titre_texte,
                sous_thematique=sous_them_texte,
            )
            records = _load_records()
            duplicate = find_row_by_uid(records, fields["uid"])
            if duplicate:
                raise SourceImportError(f"Cet identifiant existe déjà: {_row_label(duplicate)}")

            created = _grist_client().add_records([{"fields": fields}])
            st.success(f"Ligne créée (record {created[0]}): {fields['id_extraction']} → {fields['source_corpus']} / {fields['type_id']}")
            st.info("Le texte sera récupéré au prochain run du pipeline correspondant.")
        except (SourceImportError, GristError) as exc:
            st.error(str(exc))
