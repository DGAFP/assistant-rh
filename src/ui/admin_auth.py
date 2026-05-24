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

ADMIN_GROUP = "dgafpallianceadmin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

_GROUP_DISPLAY = {
    "dgafpallianceadmin": ("🔧", "#6366f1", "Admin"),
    "dgafpsd1": ("🏛️", "#8b5cf6", "DGAFP SD1"),
    "mattecentrale": ("🏢", "#f97316", "MATTE Centrale"),
    "mattedreal": ("🌍", "#f97316", "MATTE DREAL"),
    "cisirh": ("📊", "#eab308", "CISIRH"),
    "specloiret": ("📍", "#10b981", "Loiret"),
    "betatest-jan26": ("🧪", "#3b82f6", "Beta"),
    "default": ("👤", "#6b7280", "Non assigné"),
}


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
    """Check if the current user is an admin (via cookie group or session flag)."""
    if st.session_state.get("admin_authenticated"):
        return True

    cookies = _get_cookies()
    if cookies and cookies.get("user_group") == ADMIN_GROUP:
        st.session_state.admin_authenticated = True
        return True

    return False


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
    icon, color, label = _GROUP_DISPLAY.get(group, ("👤", "#6b7280", group))
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
