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
from src.ui.user_groups_store import init_user_groups_table_with_status, is_admin_group  # noqa: E402

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
_ADMIN_SECURITY_VERSION = 1


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


def _clear_cached_admin_authentication() -> None:
    """Discard both legacy and provenance-aware authorization decisions."""
    st.session_state.pop("admin_authenticated", None)
    st.session_state.pop("admin_auth_method", None)
    st.session_state.pop("_is_admin_cache", None)


def initialize_admin_security() -> None:
    """Repair group invariants before any cached authorization is trusted."""
    if st.session_state.get("_admin_security_version") == _ADMIN_SECURITY_VERSION:
        return

    result = init_user_groups_table_with_status()
    if result.default_admin_repaired:
        _clear_cached_admin_authentication()
    if result.initialized:
        st.session_state.user_groups_initialized = True
        st.session_state._admin_security_version = _ADMIN_SECURITY_VERSION


def is_admin() -> bool:
    """Check if the current user is an admin.

    Admin status comes from the group's ``is_admin`` flag in the store (not a
    hardcoded slug), so groups an admin marks as admin in #200 are honoured.
    The result is cached per-group in session state to avoid a DB round-trip on
    every chatbot rerun; the password-fallback path (``require_admin``) sets
    ``admin_authenticated`` directly and short-circuits here.
    """
    initialize_admin_security()

    auth_method = st.session_state.get("admin_auth_method")
    if st.session_state.get("admin_authenticated") and auth_method == "password":
        return True

    group = _current_group()
    cache = st.session_state.get("_is_admin_cache")
    if st.session_state.get("admin_authenticated") and auth_method == "group" and cache and cache.get("group") == group:
        return cache["value"]

    # Sessions created before auth provenance was recorded must be checked
    # against the repaired database instead of trusting a bare cached boolean.
    if st.session_state.get("admin_authenticated"):
        _clear_cached_admin_authentication()

    value = bool(group and is_admin_group(group))
    st.session_state["_is_admin_cache"] = {"group": group, "value": value}
    if value:
        st.session_state.admin_authenticated = True
        st.session_state.admin_auth_method = "group"
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
                st.session_state.admin_auth_method = "password"
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
