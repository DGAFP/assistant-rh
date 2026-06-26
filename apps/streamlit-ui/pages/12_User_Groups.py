"""Gestion des groupes utilisateurs — page réservée aux administrateurs.

Permet de créer, modifier et supprimer des groupes et de gérer leurs mots de
passe. Les groupes sont persistés en base (table ``user_groups``) ; un groupe
créé ici est immédiatement disponible dans le sélecteur de la page d'accueil.
"""

import pandas as pd
import streamlit as st

from src.ui.admin_auth import require_admin, show_admin_badge
from src.ui.user_groups_store import (
    PROTECTED_SLUGS,
    create_group,
    delete_group,
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


# ─────────────────────────────────────────────────────────────────────────────
# Liste des groupes
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("Groupes existants")
if groups:
    df = pd.DataFrame(
        [
            {
                "Icône": g["icon"],
                "Slug": g["slug"],
                "Libellé": g["label"],
                "Priorité": g["priority"],
                "Admin": "✅" if g["is_admin"] else "",
                "Mot de passe": "🔒" if g["has_password"] else "⚠️ aucun",
            }
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
        with st.form("edit_group"):
            e1, e2 = st.columns(2)
            with e1:
                e_label = st.text_input("Libellé", value=current["label"])
                e_icon = st.text_input("Icône (emoji)", value=current["icon"])
                e_priority = st.number_input("Priorité", min_value=0, value=int(current["priority"]), step=10)
            with e2:
                e_is_admin = st.checkbox("Groupe administrateur", value=bool(current["is_admin"]))
                e_color = st.color_picker("Couleur (badge)", value=current["color"] or "#6b7280")
                e_chart_color = st.color_picker("Couleur (graphiques)", value=current["chart_color"] or "#888888")
            saved = st.form_submit_button("Enregistrer", type="primary")

        if saved:
            ok, err = update_group(
                edit_slug,
                label=e_label,
                icon=e_icon,
                priority=int(e_priority),
                is_admin=e_is_admin,
                color=e_color,
                chart_color=e_chart_color,
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
