"""
Chatbot Feedback - Composants de feedback utilisateur.

Extrait de 01_Chatbot.py pour plus de lisibilité.
"""

import datetime as dt
from typing import TYPE_CHECKING

import streamlit as st

# Import logging functions
from .chatbot_logging import log_feedback_row, turn_index_by_id
from .chatbot_sources import is_negative_response

if TYPE_CHECKING:
    from .chatbot_logging import Turn


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

FEEDBACK_REASONS_NEGATIVE = [
    "Réponse incorrecte / hors-sujet / hallucination",
    "Informations incomplètes / manquantes / obsolètes",
    "Style / ton inadapté",
]

FEEDBACK_REASONS_POSITIVE_V2 = ["Clair", "Utile", "Pertinent", "Complet", "Précis"]
FEEDBACK_REASONS_NEGATIVE_V2 = ["Confus", "Éléments faux", "Non pertinent", "Incomplet", "Sources manquantes"]


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def is_feedback_pending() -> bool:
    """Vérifie si un feedback est en attente (étoiles sélectionnées mais pas soumis)."""
    if not st.session_state.get("turns"):
        return False
    
    last_turn = st.session_state.turns[-1]
    tid = last_turn.id
    
    has_selected_stars = st.session_state.get(f"feedback_stars_{tid}") is not None
    feedback_submitted = st.session_state.get(f"fb_sub_{tid}", False)
    is_negative = is_negative_response(last_turn.assistant)
    
    return has_selected_stars and not feedback_submitted and not is_negative


# ═══════════════════════════════════════════════════════════════════════════════
# VERSION 1: Feedback simple (original)
# ═══════════════════════════════════════════════════════════════════════════════

def render_feedback_block_v1(turn: "Turn") -> None:
    """Version simple du feedback avec thumbs up/down."""
    tid = turn.id
    already = bool(turn.feedback)
    submitted = st.session_state.get(f"fb_sub_{tid}", False)

    if already or submitted:
        st.caption("Merci pour votre retour 🙏")
        return

    st.markdown("**Cette réponse vous a‑t‑elle été utile ?**")
    c1, c2, c3 = st.columns([1, 1, 4])

    if c1.button("👍 Oui", key=f"up_{tid}", width="stretch"):
        turn.feedback = {"rating": "up", "at": dt.datetime.now(dt.UTC).isoformat()}
        st.session_state[f"fb_sub_{tid}"] = True
        idx = turn_index_by_id(tid)
        log_feedback_row({
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "turn_id": tid,
            "turn_idx": idx if idx is not None else "",
            "helpful": True,
            "reasons": "",
            "reasons_positive": "",
            "reasons_negative": "",
            "comment": "",
            "stars": None,
            "session_id": st.session_state.get("session_id", ""),
            "question": turn.user,
            "answer": turn.assistant,
        })
        st.rerun()
        
    if c2.button("👎 Non", key=f"down_{tid}", width="stretch"):
        st.session_state[f"fb_show_{tid}"] = True
        st.rerun()

    if st.session_state.get(f"fb_show_{tid}", False):
        with st.expander("Aidez‑nous à améliorer cette réponse", expanded=True):
            chosen = []
            for i, label in enumerate(FEEDBACK_REASONS_NEGATIVE):
                if st.checkbox(label, key=f"r_{tid}_{i}"):
                    chosen.append(label)
            comment = st.text_area("Commentaires (optionnel)", key=f"c_{tid}", placeholder="")
            if st.button("Envoyer", key=f"s_{tid}"):
                turn.feedback = {
                    "rating": "down",
                    "reasons": chosen,
                    "comment": comment.strip() or None,
                    "at": dt.datetime.now(dt.UTC).isoformat(),
                }
                st.session_state[f"fb_sub_{tid}"] = True
                idx = turn_index_by_id(tid)
                log_feedback_row({
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "turn_id": tid,
                    "turn_idx": idx if idx is not None else "",
                    "helpful": False,
                    "reasons": "; ".join(chosen),
                    "reasons_positive": "",
                    "reasons_negative": "; ".join(chosen),
                    "comment": (comment or "").strip(),
                    "stars": None,
                    "session_id": st.session_state.get("session_id", ""),
                    "question": turn.user,
                    "answer": turn.assistant,
                })
                st.toast("Merci pour votre retour 🙏")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# VERSION 2: Feedback enrichi (PM style)
# ═══════════════════════════════════════════════════════════════════════════════

def render_feedback_block_v2(turn: "Turn") -> None:
    """
    Version enrichie du feedback avec notation par étoiles et raisons positives/négatives.
    Design demandé par la Product Manager - Utilise le widget natif st.feedback().
    """
    tid = turn.id
    already = bool(turn.feedback)
    submitted = st.session_state.get(f"fb_sub_{tid}", False)

    if already or submitted:
        st.caption("Merci pour votre retour 🙏")
        return

    st.markdown("**Comment évaluez-vous la réponse ?**")
    
    # Widget natif st.feedback avec des étoiles (retourne 0-4, None si rien sélectionné)
    selected = st.feedback("stars", key=f"feedback_stars_{tid}")
    
    if selected is not None:
        # Logique selon le nombre d'étoiles
        if selected <= 1:
            # 1-2 étoiles : négatif seulement
            show_negative = True
            show_positive = False
            helpful = False
        elif selected >= 4:
            # 5 étoiles : positif seulement
            show_negative = False
            show_positive = True
            helpful = True
        else:
            # 3-4 étoiles : mitigé
            show_negative = True
            show_positive = True
            helpful = True
        
        with st.expander("Aidez-nous à améliorer cette réponse", expanded=True):
            chosen_positive = []
            chosen_negative = []
            
            if show_positive:
                st.markdown("👍 **Qu'avez-vous particulièrement apprécié ?**")
                cols_pos = st.columns(5)
                for idx, reason in enumerate(FEEDBACK_REASONS_POSITIVE_V2):
                    with cols_pos[idx]:
                        if st.checkbox(reason, key=f"pos_{tid}_{idx}"):
                            chosen_positive.append(reason)
            
            if show_negative:
                if show_positive:
                    st.markdown("")
                st.markdown("👎 **Qu'est-ce qui pourrait être amélioré ?**")
                cols_neg = st.columns(5)
                for idx, reason in enumerate(FEEDBACK_REASONS_NEGATIVE_V2):
                    with cols_neg[idx]:
                        if st.checkbox(reason, key=f"neg_{tid}_{idx}"):
                            chosen_negative.append(reason)
            
            comment = st.text_area(
                "Commentaires", 
                key=f"comment_{tid}", 
                placeholder="Partagez vos suggestions pour améliorer cette réponse..."
            )
            
            has_reasons = len(chosen_positive) > 0 or len(chosen_negative) > 0
            has_comment = bool(comment and comment.strip())
            has_feedback_content = has_reasons or has_comment
            
            if st.button("Envoyer", key=f"submit_{tid}"):
                if not has_feedback_content:
                    st.session_state[f"fb_attempt_{tid}"] = True
                    st.rerun()
                else:
                    all_reasons = chosen_positive + chosen_negative
                    turn.feedback = {
                        "stars": selected,
                        "helpful": helpful,
                        "reasons": all_reasons,
                        "reasons_positive": chosen_positive,
                        "reasons_negative": chosen_negative,
                        "comment": comment.strip() or None,
                        "at": dt.datetime.now(dt.UTC).isoformat(),
                    }
                    st.session_state[f"fb_sub_{tid}"] = True
                    
                    idx = turn_index_by_id(tid)
                    log_feedback_row({
                        "ts": dt.datetime.now(dt.UTC).isoformat(),
                        "turn_id": tid,
                        "turn_idx": idx if idx is not None else "",
                        "helpful": helpful,
                        "reasons": "; ".join(all_reasons) if all_reasons else "",
                        "reasons_positive": "; ".join(chosen_positive) if chosen_positive else "",
                        "reasons_negative": "; ".join(chosen_negative) if chosen_negative else "",
                        "comment": (comment or "").strip(),
                        "stars": selected,
                        "session_id": st.session_state.get("session_id", ""),
                        "question": turn.user,
                        "answer": turn.assistant,
                    })
                    
                    st.toast("Merci pour votre retour 🙏")
                    st.rerun()
            
            if st.session_state.get(f"fb_attempt_{tid}", False) and not has_feedback_content:
                st.warning("⚠️ Veuillez cocher au moins une raison ou écrire un commentaire pour envoyer votre feedback")
            
            if has_feedback_content and st.session_state.get(f"fb_attempt_{tid}", False):
                st.session_state[f"fb_attempt_{tid}"] = False


# ═══════════════════════════════════════════════════════════════════════════════
# WRAPPER
# ═══════════════════════════════════════════════════════════════════════════════

def render_feedback_block(turn: "Turn") -> None:
    """Wrapper qui choisit la version de feedback selon la config."""
    use_v2 = st.session_state.get("use_feedback_v2", True)
    if use_v2:
        render_feedback_block_v2(turn)
    else:
        render_feedback_block_v1(turn)

