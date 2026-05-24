# ruff: noqa: I001
# Suppress deprecation warnings before any streamlit import
import warnings
warnings.filterwarnings("ignore", message=".*st.cache.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*st.cache.*", category=FutureWarning)

import streamlit as st  # noqa: E402

from src.ui.cookies_security import resolve_cookies_password  # noqa: E402

if not hasattr(st, "_original_cache"):
    st._original_cache = getattr(st, "cache", None)

    def _compat_cache(func=None, **_kwargs):
        return st.cache_resource(func)

    st.cache = _compat_cache

# Validate cookie secret policy at startup (fail fast in staging/prod)
resolve_cookies_password()


# Hide sidebar before redirect
st.set_page_config(page_title="Assistant RH", page_icon="📚", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
    <style>
[data-testid="stSidebar"] { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }
section[data-testid="stSidebarNav"] { display: none !important; }
    </style>
""", unsafe_allow_html=True)

# Redirect directly to Chatbot page
st.switch_page("pages/01_Chatbot.py")
