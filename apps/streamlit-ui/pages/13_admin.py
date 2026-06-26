"""Raccourci ``/admin`` → redirige vers l'espace d'administration.

Streamlit sert chaque page sous une URL dérivée de son nom de fichier (préfixe
``NN_`` retiré), donc ce fichier est accessible à ``/admin``. Pratique depuis
que le groupe Admin n'est plus proposé dans le sélecteur de la page d'accueil :
un admin tape ``/admin`` et s'authentifie via ``require_admin`` si besoin.

L'accès reste protégé — ``require_admin()`` affiche le formulaire de mot de
passe (ADMIN_PASSWORD) quand l'utilisateur n'est pas déjà administrateur.
"""

import streamlit as st

from src.ui.admin_auth import require_admin

# Page d'administration cible du raccourci (modifiable).
_ADMIN_LANDING = "pages/04_Admin_Config.py"

st.set_page_config(page_title="Admin", page_icon="🔧", layout="wide")

require_admin()
st.switch_page(_ADMIN_LANDING)
