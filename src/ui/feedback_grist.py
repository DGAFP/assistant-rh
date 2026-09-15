"""Admin dashboard controls for the package's manual Grist synchronization."""

from datetime import datetime

import pandas as pd
import streamlit as st
from assistant_rh_rag_pipeline.feedback_grist import (
    FeedbackGristClient,
    FeedbackGristConfig,
    FeedbackGristError,
    feedback_source_environment,
)

from src.ui.feedback_dashboard import PARIS_TIMEZONE


def render_feedback_grist_sync(filtered: pd.DataFrame, history: pd.DataFrame, period: str) -> None:
    with st.expander("Synchroniser les feedbacks vers Grist"):
        st.caption(
            "Synchronisation manuelle : ajoute les nouveaux feedbacks et actualise les données sources. "
            "Hors-champs, missing_document et Traité sont réservés à la revue humaine et conservés à chaque relance."
        )
        try:
            config = FeedbackGristConfig.from_env()
            environment = feedback_source_environment()
        except FeedbackGristError as exc:
            st.info(str(exc))
            return

        scope = st.radio("Feedbacks à synchroniser", ("Filtres actuels du dashboard", "Tout l’historique"), key="fb_grist_scope")
        # Use the raw values for both scopes: display processing fills missing
        # groups and changes timestamps, and must not change replicated fields.
        data = history
        if scope != "Tout l’historique":
            identifiers = filtered["id"] if "id" in filtered else []
            data = history[history["id"].isin(identifiers)] if "id" in history else history.iloc[0:0]
        count = data["id"].nunique() if "id" in data else 0
        st.write(f"**{count} feedback(s) — environnement : {environment}**")
        st.caption("Tout l’historique inclut les groupes masqués et les feedbacks sans run associé." if scope == "Tout l’historique" else period)
        st.write(f"Destination : [{config.doc_id}]({config.document_url}) · table **{config.table_id}** (créée si absente).")
        st.caption("Pour récupérer les dernières données et analyses, utilisez « Rafraîchir » avant de synchroniser.")

        if st.button("Synchroniser vers Grist", key="fb_grist_sync", disabled=count == 0):
            with st.spinner("Synchronisation des feedbacks…"):
                try:
                    result = FeedbackGristClient(config).sync(data, environment)
                    error = result.error
                    message = f"{result.synced}/{result.total} feedback(s) transmis et confirmés par Grist."
                except FeedbackGristError as exc:
                    error = str(exc)
                    message = "Synchronisation annulée avant envoi."
                st.session_state["fb_grist_result"] = {
                    "error": error,
                    "message": message,
                    "context": (
                        f"{datetime.now(PARIS_TIMEZONE):%d/%m/%Y %H:%M:%S} · {environment} · {config.doc_id}/{config.table_id} · {scope} · {period}"
                    ),
                }

        if previous := st.session_state.get("fb_grist_result"):
            st.caption(f"Dernière tentative dans cette session : {previous['context']}")
            if previous["error"]:
                st.error(f"{previous['message']} {previous['error']}")
                st.info(
                    "Un lot non confirmé a pu être reçu. Relancez le même périmètre : les lignes existantes et les annotations seront conservées."
                )
            else:
                st.success(previous["message"])
