"""Gestion des groupes utilisateurs — page réservée aux administrateurs.

Permet de créer, modifier et supprimer des groupes et de gérer leurs mots de
passe. Les groupes sont persistés en base (table ``user_groups``) ; un groupe
créé ici est immédiatement disponible dans le sélecteur de la page d'accueil.
"""

import pandas as pd
import streamlit as st
from assistant_rh_rag_pipeline.ministry_scope import MINISTRY_CATALOG

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.user_groups_store import (
    PROTECTED_SLUGS,
    create_group,
    delete_group,
    group_policy_status,
    init_user_groups_table,
    list_groups,
    set_password,
    update_group,
)

st.set_page_config(page_title="Groupes utilisateurs", page_icon="👥", layout="wide")

require_admin()
show_admin_badge()

init_user_groups_table()

st.title("👥 Gestion des groupes utilisateurs")
st.caption(
    "Créez, modifiez et supprimez les groupes, et gérez leurs mots de passe. "
    "Chaque groupe doit avoir un mot de passe pour être utilisable depuis la page d'accueil."
)

groups = list_groups()
_by_slug = {g["slug"]: g for g in groups}
MINISTRY_OPTIONS = list(MINISTRY_CATALOG)


def _ministry_label(ministry_id: str) -> str:
    ministry = MINISTRY_CATALOG.get(ministry_id)
    return ministry.label if ministry else ministry_id


def _policy_display(group: dict) -> tuple[str, str, str]:
    policy = group_policy_status(group)
    allowed = ", ".join(_ministry_label(m) for m in policy["allowed_ministries"]) or "—"
    default = _ministry_label(policy["default_ministry"]) if policy["default_ministry"] else "—"
    status = "✅" if policy["valid"] else f"⚠️ {policy['error']}"
    return allowed, default, status


# ─────────────────────────────────────────────────────────────────────────────
# Liste des groupes
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("Groupes existants")
if groups:
    df = pd.DataFrame(
        [
            (
                lambda allowed, default, status: {
                    "Icône": g["icon"],
                    "Slug": g["slug"],
                    "Libellé": g["label"],
                    "Priorité": g["priority"],
                    "Admin": "✅" if g["is_admin"] else "",
                    "Visible": "✅" if g.get("visible", True) else "🚫 masqué",
                    "Ministères": allowed,
                    "Défaut": default,
                    "Politique": status,
                    "Mot de passe": "🔒" if g["has_password"] else "⚠️ aucun",
                }
            )(*_policy_display(g))
            for g in groups
        ]
    )
    st.dataframe(df, hide_index=True, use_container_width=True)
else:
    st.info("Aucun groupe. Créez-en un ci-dessous.")


tab_create, tab_edit, tab_password, tab_delete = st.tabs(["➕ Créer", "✏️ Modifier", "🔑 Mot de passe", "🗑️ Supprimer"])


# ─────────────────────────────────────────────────────────────────────────────
# Créer
# ─────────────────────────────────────────────────────────────────────────────
with tab_create:
    with st.form("create_group"):
        c1, c2 = st.columns(2)
        with c1:
            slug = st.text_input("Slug *", help="Minuscules, chiffres et tirets (ex. : mattecentrale)")
            label = st.text_input("Libellé *", help="Nom affiché (ex. : MATTE Centrale)")
            password = st.text_input("Mot de passe *", type="password")
        with c2:
            icon = st.text_input("Icône (emoji)", value="👥")
            priority = st.number_input("Priorité", min_value=0, value=0, step=10)
            is_admin = st.checkbox("Groupe administrateur", value=False)
            visible = st.checkbox(
                "Visible dans le sélecteur", value=True, help="Décocher pour masquer le groupe sur la page d'accueil (il reste en base)."
            )
        allowed_ministries = st.multiselect(
            "Ministères autorisés *",
            options=MINISTRY_OPTIONS,
            default=["matte"],
            format_func=_ministry_label,
            help="Le groupe pourra sélectionner un seul de ces ministères pour chaque requête RAG.",
        )
        default_options = allowed_ministries or ["matte"]
        default_ministry = st.selectbox(
            "Ministère par défaut *",
            options=default_options,
            index=0,
            format_func=_ministry_label,
        )
        c3, c4 = st.columns(2)
        with c3:
            color = st.color_picker("Couleur (badge)", value="#6b7280")
        with c4:
            chart_color = st.color_picker("Couleur (graphiques)", value="#888888")
        submitted = st.form_submit_button("Créer le groupe", type="primary")

    if submitted:
        ok, err = create_group(
            slug,
            label,
            password,
            icon=icon,
            color=color,
            priority=int(priority),
            is_admin=is_admin,
            visible=visible,
            allowed_ministries=allowed_ministries,
            default_ministry=default_ministry,
            chart_color=chart_color,
        )
        if ok:
            st.success(f"Groupe « {slug.strip().lower()} » créé.")
            st.rerun()
        else:
            st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# Modifier
# ─────────────────────────────────────────────────────────────────────────────
with tab_edit:
    if not groups:
        st.info("Aucun groupe à modifier.")
    else:
        edit_slug = st.selectbox(
            "Groupe à modifier",
            options=[g["slug"] for g in groups],
            format_func=lambda s: f"{_by_slug[s]['icon']} {_by_slug[s]['label']} ({s})",
            key="edit_select",
        )
        current = _by_slug[edit_slug]
        current_policy = group_policy_status(current)
        if not current_policy["valid"]:
            st.warning(f"Politique ministérielle invalide : {current_policy['error']}")
        current_allowed = [m for m in current_policy["allowed_ministries"] if m in MINISTRY_CATALOG] or ["matte"]
        current_default = current_policy["default_ministry"] if current_policy["default_ministry"] in current_allowed else current_allowed[0]
        with st.form("edit_group"):
            e1, e2 = st.columns(2)
            with e1:
                e_label = st.text_input("Libellé", value=current["label"])
                e_icon = st.text_input("Icône (emoji)", value=current["icon"])
                e_priority = st.number_input("Priorité", min_value=0, value=int(current["priority"]), step=10)
            with e2:
                e_is_admin = st.checkbox("Groupe administrateur", value=bool(current["is_admin"]))
                e_visible = st.checkbox("Visible dans le sélecteur", value=bool(current.get("visible", True)))
                e_color = st.color_picker("Couleur (badge)", value=current["color"] or "#6b7280")
                e_chart_color = st.color_picker("Couleur (graphiques)", value=current["chart_color"] or "#888888")
            e_allowed_ministries = st.multiselect(
                "Ministères autorisés *",
                options=MINISTRY_OPTIONS,
                default=current_allowed,
                format_func=_ministry_label,
            )
            e_default_options = e_allowed_ministries or current_allowed
            e_default_index = e_default_options.index(current_default) if current_default in e_default_options else 0
            e_default_ministry = st.selectbox(
                "Ministère par défaut *",
                options=e_default_options,
                index=e_default_index,
                format_func=_ministry_label,
            )
            saved = st.form_submit_button("Enregistrer", type="primary")

        if saved:
            ok, err = update_group(
                edit_slug,
                label=e_label,
                icon=e_icon,
                priority=int(e_priority),
                is_admin=e_is_admin,
                visible=e_visible,
                color=e_color,
                chart_color=e_chart_color,
                allowed_ministries=e_allowed_ministries,
                default_ministry=e_default_ministry,
            )
            if ok:
                st.success(f"Groupe « {edit_slug} » mis à jour.")
                st.rerun()
            else:
                st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# Mot de passe
# ─────────────────────────────────────────────────────────────────────────────
with tab_password:
    if not groups:
        st.info("Aucun groupe.")
    else:
        pwd_slug = st.selectbox(
            "Groupe",
            options=[g["slug"] for g in groups],
            format_func=lambda s: f"{_by_slug[s]['icon']} {_by_slug[s]['label']} ({s})",
            key="pwd_select",
        )
        with st.form("reset_password"):
            new_pwd = st.text_input("Nouveau mot de passe", type="password")
            reset = st.form_submit_button("Réinitialiser le mot de passe", type="primary")
        if reset:
            ok, err = set_password(pwd_slug, new_pwd)
            if ok:
                st.success(f"Mot de passe du groupe « {pwd_slug} » réinitialisé.")
            else:
                st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# Supprimer
# ─────────────────────────────────────────────────────────────────────────────
with tab_delete:
    deletable = [g["slug"] for g in groups if g["slug"] not in PROTECTED_SLUGS]
    if not deletable:
        st.info("Aucun groupe supprimable (les groupes structurels sont protégés).")
    else:
        del_slug = st.selectbox(
            "Groupe à supprimer",
            options=deletable,
            format_func=lambda s: f"{_by_slug[s]['icon']} {_by_slug[s]['label']} ({s})",
            key="del_select",
        )
        st.warning(f"La suppression du groupe « {del_slug} » est définitive.")
        confirm = st.checkbox(f"Je confirme la suppression de « {del_slug} »", key="del_confirm")
        if st.button("Supprimer définitivement", type="primary", disabled=not confirm):
            ok, err = delete_group(del_slug)
            if ok:
                st.success(f"Groupe « {del_slug} » supprimé.")
                st.rerun()
            else:
                st.error(err)
