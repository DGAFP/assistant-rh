"""
Admin authentication for Streamlit pages.

Two-tier access control:
  1. Cookie-based: users with the "dgafpallianceadmin" group cookie
     (set via ?group=dgafpallianceadmin URL param) skip the password.
  2. Password fallback: all other users must enter the admin password.

Usage in any page::

    from src.ui.admin_auth import require_admin
    require_admin()   # blocks with st.stop() if not authorized
    show_admin_badge() # optional sidebar badge
"""

from __future__ import annotations

import os
import warnings

from dotenv import load_dotenv

load_dotenv()

warnings.filterwarnings("ignore", message=".*st.cache.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*st.cache.*", category=FutureWarning)

import streamlit as st  # noqa: E402

if not hasattr(st, "_original_cache"):
    st._original_cache = getattr(st, "cache", None)

    def _compat_cache(func=None, **_kwargs):
        return st.cache_resource(func)

    st.cache = _compat_cache

from streamlit_cookies_manager import EncryptedCookieManager  # noqa: E402

from src.ui.cookies_security import resolve_cookies_password  # noqa: E402
from src.ui.groups import ADMIN_GROUP, DEFAULT_BADGE, badge_display  # noqa: E402, F401
from src.ui.user_groups_store import is_admin_group  # noqa: E402

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


def _get_cookies() -> EncryptedCookieManager | None:
    """Return the cookie manager if it's ready, or None."""
    try:
        cookies = EncryptedCookieManager(
            prefix="assistant_rh_",
            password=resolve_cookies_password(),
        )
        if not cookies.ready():
            return None
        return cookies
    except Exception:
        return None


def _current_group() -> str:
    """Return the current user group from session state or cookies."""
    group = st.session_state.get("user_group")
    if group:
        return group
    cookies = _get_cookies()
    if cookies:
        group = cookies.get("user_group", "default")
        st.session_state.user_group = group
        return group
    return "default"


def is_admin() -> bool:
    """Check if the current user is an admin.

    Admin status comes from the group's ``is_admin`` flag in the store (not a
    hardcoded slug), so groups an admin marks as admin in #200 are honoured.
    The result is cached per-group in session state to avoid a DB round-trip on
    every chatbot rerun; the password-fallback path (``require_admin``) sets
    ``admin_authenticated`` directly and short-circuits here.
    """
    if st.session_state.get("admin_authenticated"):
        return True

    group = _current_group()
    cache = st.session_state.get("_is_admin_cache")
    if cache and cache.get("group") == group:
        return cache["value"]

    value = bool(group and is_admin_group(group))
    st.session_state["_is_admin_cache"] = {"group": group, "value": value}
    if value:
        st.session_state.admin_authenticated = True
    return value


def require_admin() -> None:
    """
    Gate the current page behind admin authentication.

    If the user is already authenticated (cookie or session), returns immediately.
    Otherwise, shows a password form and calls ``st.stop()``.
    """
    if is_admin():
        return

    st.markdown("## 🔐 Accès Admin Requis")
    st.markdown("Cette page est réservée aux administrateurs.")

    with st.form("admin_login"):
        password = st.text_input("Mot de passe admin", type="password")
        submit = st.form_submit_button("Se connecter", type="primary")

        if submit:
            if password == ADMIN_PASSWORD:
                st.session_state.admin_authenticated = True
                st.rerun()
            else:
                st.error("❌ Mot de passe incorrect")

    st.stop()


def show_admin_badge() -> None:
    """Render a small group/session badge in the sidebar (admin pages only)."""
    group = _current_group()
    icon, color, label = badge_display().get(group, (*DEFAULT_BADGE, group))
    session_id = st.session_state.get("session_id", "N/A")

    # Show how the user is authenticated
    if group == ADMIN_GROUP:
        auth_hint = "cookie"
    elif st.session_state.get("admin_authenticated"):
        auth_hint = "mot de passe"
    else:
        auth_hint = ""

    auth_line = f' <span style="color:#888;font-size:0.75em;">({auth_hint})</span>' if auth_hint else ""

    with st.sidebar:
        st.markdown(
            f"""
        <div style="
            background: linear-gradient(135deg, {color}22, {color}11);
            border-left: 3px solid {color};
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 16px;
            font-size: 0.85em;
        ">
            {icon} <strong>Groupe:</strong> {label}{auth_line}
            <br><span style="color: #888; font-size: 0.8em;">Session: {session_id}</span>
        </div>
        """,
            unsafe_allow_html=True,
        )
