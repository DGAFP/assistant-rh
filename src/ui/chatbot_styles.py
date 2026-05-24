"""
Chatbot Styles - CSS et styles DSFR pour le chatbot.

Extrait de 01_Chatbot.py pour plus de lisibilité.
"""

import streamlit as st

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

DSFR_COLORS = {
    "blue_france": "#003091",
    "violet_france": "#696AF4",
    "red_marianne": "#E10110",
    "green_emeraude": "#18753C",
    "grey_950": "#161616",
    "grey_200": "#E5E5E5",
    "grey_50": "#F6F6F6",
}


# ═══════════════════════════════════════════════════════════════════════════════
# CSS
# ═══════════════════════════════════════════════════════════════════════════════

CHATBOT_CSS = """<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20,400,0,0" rel="stylesheet"><style>
/* ====== HIDE ONLY THE PAGE NAVIGATION (keep sidebar content) ====== */
/* Hide the automatic Streamlit page navigation links */
[data-testid="stSidebarNav"] { display: none !important; }
nav[data-testid="stSidebarNav"] { display: none !important; }
/* Target the navigation list specifically */
[data-testid="stSidebarNavItems"] { display: none !important; }
ul[data-testid="stSidebarNavItems"] { display: none !important; }
/* Hide any nav element in sidebar */
[data-testid="stSidebar"] nav { display: none !important; }
[data-testid="stSidebar"] [data-testid="stSidebarNavSeparator"] { display: none !important; }
/* Hide deprecation warnings in UI */
.stException, .stWarning:has-text("deprecated"), div[data-testid="stNotification"]:has-text("cache") { display: none !important; }

:root {
    --blue-france: #003091;
    --violet-france: #696AF4;
    --red-marianne: #E10110;
    --green-emeraude: #18753C;
    --grey-950: #161616;
    --grey-200: #E5E5E5;
    --grey-50: #F6F6F6;
}

/* Chat messages */
div.stChatMessage > div { padding: 0.6rem 0.8rem; }
details > summary { font-weight: 600; color: var(--grey-950); }

/* Focus states */
.st-emotion-cache-yd4u6l:focus-within {border-color: var(--blue-france);}
.st-emotion-cache-1yk2xem:focus-within {border-color: var(--blue-france);}

/* Chunk cards */
.chunk-card { 
    border: 1px solid var(--grey-200); 
    border-radius: 8px; 
    padding: 0.6rem 0.8rem; 
    margin-bottom: 0.5rem; 
    background: var(--grey-50); 
}
.chunk-card-used { 
    border: 2px solid #10b981; 
    border-radius: 8px; 
    padding: 0.6rem 0.8rem; 
    margin-bottom: 0.5rem; 
    background: #f0fdf4; 
    box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.1); 
}

/* Badges */
.badge { 
    display: inline-block; 
    padding: 2px 8px; 
    border-radius: 999px; 
    font-size: 12px; 
    background: #eef2ff; 
    color: #4f46e5; 
    font-weight: 500; 
}
.muted { color: #666666; font-size: 12px; }

/* Buttons */
.stButton > button[kind="primary"] { 
    background-color: var(--blue-france) !important; 
    color: white !important; 
    border: none !important; 
    font-weight: 500 !important; 
    transition: background-color 0.2s ease; 
}
.stButton > button[kind="primary"]:hover { 
    background-color: #0041b3 !important; 
}
.stButton > button { 
    border-radius: 4px !important; 
    font-weight: 500 !important; 
}

/* Text input */
.stTextInput > div > div > input:focus { 
    border-color: var(--blue-france) !important; 
    box-shadow: 0 0 0 1px var(--blue-france) !important; 
}

/* DSFR Header */
.dsfr-header { 
    display: flex; 
    align-items: center; 
    gap: 16px; 
    margin-bottom: 24px; 
    padding-bottom: 16px; 
    border-bottom: 2px solid var(--grey-200); 
}
.dsfr-accent-bar { 
    width: 4px; 
    height: 48px; 
    background: var(--blue-france); 
    border-radius: 2px; 
}
.dsfr-title { 
    margin: 0; 
    color: var(--grey-950); 
    font-size: 2rem; 
    font-weight: 700; 
    line-height: 1.2; 
}
.dsfr-subtitle { 
    margin: 4px 0 0 0; 
    color: #666666; 
    font-size: 0.875rem; 
    font-weight: 400; 
}

/* Welcome section */
.dsfr-welcome-title { 
    margin: 0 0 8px 0; 
    font-weight: 600; 
    color: var(--grey-950); 
    font-size: 1rem; 
}
.dsfr-welcome-warning { 
    margin: 8px 0; 
    padding: 8px 12px; 
    background: #FFF5E6; 
    border-left: 3px solid #FF9940; 
    border-radius: 4px; 
    color: var(--grey-950); 
    font-size: 0.875rem; 
}
.dsfr-welcome-text { 
    margin: 8px 0 0 0; 
    color: var(--grey-950); 
    font-size: 0.9375rem; 
}

/* Material icons */
.material-symbols-outlined { 
    font-family: 'Material Symbols Outlined'; 
    font-weight: normal; 
    font-style: normal; 
    font-size: 24px; 
    line-height: 1; 
    letter-spacing: normal; 
    text-transform: none; 
    display: inline-block; 
    white-space: nowrap; 
    word-wrap: normal; 
    direction: ltr; 
    vertical-align: middle; 
}

/* Suggestion buttons */
button[kind="secondary"] .st-emotion-cache-12j140x {
    width: 100% !important;
    text-align: left !important;
    padding: 8px 12px !important;
    border-radius: 4px !important;
    border: none !important;
    border-left: 3px solid var(--blue-france) !important;
    background: #f0f2ff !important;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05) !important;
    transition: all 0.2s ease !important;
    min-height: 32px !important;
    height: auto !important;
    margin-bottom: 0px !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x p {
    font-size: 14px !important;
    margin: 0 !important;
    padding: 0 !important;
    white-space: normal !important;
    line-height: 1.3 !important;
    color: var(--grey-950) !important;
    font-weight: 400 !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x:hover {
    background: #e0e5ff !important;
    border-left-color: #0041b3 !important;
    box-shadow: 0 3px 6px rgba(0, 0, 145, 0.12) !important;
    cursor: pointer !important;
}
button[kind="secondary"] .st-emotion-cache-12j140x:active {
    transform: translateX(1px) !important;
    box-shadow: 0 1px 3px rgba(0, 0, 145, 0.15) !important;
}
button[kind="secondary"] {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
    margin-bottom: 0 !important;
}
button[kind="secondary"]:hover {
    background: rgba(151, 166, 195, 0.15) !important;
}

/* Layout */
.st-emotion-cache-liupih { padding: 3rem 5rem 5rem; }
.st-emotion-cache-10p9htt { height: 3.5rem !important; }
button[kind="secondary"] .st-emotion-cache-9114l4:hover { 
    background-color: var(--grey-200) !important; 
}

/* Feedback V2: Le widget natif st.feedback gère le style automatiquement */
</style>"""


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def inject_chatbot_styles():
    """Inject chatbot CSS styles into the page."""
    st.markdown(CHATBOT_CSS, unsafe_allow_html=True)


def render_dsfr_header(title: str, subtitle: str = ""):
    """Render DSFR-style header."""
    subtitle_html = f'<p class="dsfr-subtitle">{subtitle}</p>' if subtitle else ""
    st.markdown(f"""
    <div class="dsfr-header">
        <div class="dsfr-accent-bar"></div>
        <div>
            <h1 class="dsfr-title">{title}</h1>
            {subtitle_html}
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_welcome_message(title: str, warning: str = "", text: str = ""):
    """Render welcome message box."""
    html_parts = [f'<p class="dsfr-welcome-title">{title}</p>']
    if warning:
        html_parts.append(f'<p class="dsfr-welcome-warning">{warning}</p>')
    if text:
        html_parts.append(f'<p class="dsfr-welcome-text">{text}</p>')
    
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def source_badge_html(source_type: str) -> str:
    """Generate HTML for source type badge."""
    badge_styles = {
        "MATTE": ("📋", "#e0f2fe", "#0369a1"),
        "Service Public": ("📚", "#fef3c7", "#d97706"),
        "DGAFP": ("⚖️", "#f3e8ff", "#7c3aed"),
        "Légifrance": ("📜", "#fee2e2", "#dc2626"),
        "default": ("📄", "#f3f4f6", "#6b7280"),
    }
    
    icon, bg, color = badge_styles.get(source_type, badge_styles["default"])
    
    return f'''
    <span style="
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 500;
        background: {bg};
        color: {color};
    ">
        {icon} {source_type}
    </span>
    '''

