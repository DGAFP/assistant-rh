# ruff: noqa: I001
# Suppress deprecation warnings before any streamlit import
import warnings

warnings.filterwarnings("ignore", message=".*st.cache.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*st.cache.*", category=FutureWarning)

import streamlit as st  # noqa: E402

from src.ui.cookies_security import resolve_cookies_password  # noqa: E402
from src.ui.groups import ADMIN_GROUP, valid_groups  # noqa: E402
from src.ui.user_groups_store import (  # noqa: E402
    init_user_groups_table,
    list_groups,
    verify_password,
)

if not hasattr(st, "_original_cache"):
    st._original_cache = getattr(st, "cache", None)

    def _compat_cache(func=None, **_kwargs):
        return st.cache_resource(func)

    st.cache = _compat_cache

from streamlit_cookies_manager import EncryptedCookieManager  # noqa: E402

# Validate cookie secret policy at startup (fail fast in staging/production)
resolve_cookies_password()


# Hide sidebar on the landing page (user not identified yet)
st.set_page_config(page_title="Assistant RH", page_icon="📚", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
[data-testid="stSidebar"] { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }
section[data-testid="stSidebarNav"] { display: none !important; }
    </style>
""",
    unsafe_allow_html=True,
)

# Ensure the user_groups table exists and is seeded (once per session)
if "user_groups_initialized" not in st.session_state:
    init_user_groups_table()
    st.session_state.user_groups_initialized = True

cookies = EncryptedCookieManager(prefix="assistant_rh_", password=resolve_cookies_password())
if not cookies.ready():
    st.stop()


def _go_to_chatbot() -> None:
    st.switch_page("pages/01_Chatbot.py")


# Fast path 1: existing ?group=<slug> deep link (cohort onboarding) — let the
# chatbot resolve and persist it, exactly as before. Admin can't be set this way.
_url_group = st.query_params.get("group", "").lower()
if _url_group and _url_group in valid_groups() and _url_group != ADMIN_GROUP:
    _go_to_chatbot()

# Fast path 2: a returning user with a real group cookie skips the picker.
_existing = cookies.get("user_group")
if _existing and _existing in valid_groups() and _existing != "default":
    _go_to_chatbot()


# ─────────────────────────────────────────────────────────────────────────────
# Group picker (shown when no group is established yet)
# ─────────────────────────────────────────────────────────────────────────────
# "default" (Non assigné) is an internal fallback, not a real identity to pick.
_selectable = [g for g in list_groups() if g["slug"] != "default"]
_by_slug = {g["slug"]: g for g in _selectable}

st.markdown(
    """
    <div style="max-width: 560px; margin: 8vh auto 1.5rem auto; text-align: center;">
        <h1 style="margin-bottom: 0.25rem;">📚 Assistant RH</h1>
        <p style="color: #666; font-size: 0.95rem;">
            Sélectionnez votre groupe et saisissez le mot de passe associé pour accéder à l'assistant.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

_left, _center, _right = st.columns([1, 2, 1])
with _center:
    if not _selectable:
        st.error("Aucun groupe disponible. Contactez un administrateur.")
        st.stop()

    with st.form("group_picker", clear_on_submit=False):
        slug = st.selectbox(
            "Groupe",
            options=[g["slug"] for g in _selectable],
            format_func=lambda s: f"{_by_slug[s]['icon']} {_by_slug[s]['label']}".strip(),
        )
        password = st.text_input("Mot de passe", type="password")
        submitted = st.form_submit_button("Accéder à l'assistant", type="primary", use_container_width=True)

    if submitted:
        if verify_password(slug, password):
            cookies["user_group"] = slug
            cookies.save()
            # Rerun rather than switch_page directly: the cookie write needs a
            # frontend round-trip, after which fast-path 2 above redirects.
            st.rerun()
        else:
            st.error("❌ Mot de passe incorrect ou groupe non configuré.")
