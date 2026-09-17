"""Shared page setup and persistent evaluation reminder for the Streamlit UI."""

import streamlit as st

FEEDBACK_REMINDER = "Pensez à évaluer les réponses avec les étoiles : votre avis nous aide à améliorer l’assistant."

# Reserve a strip above BOTH the header and the application viewport. Unlike an
# overlay in the chat, this leaves the entire scrolling area and chat input free.
# These Streamlit DOM selectors should be checked when upgrading Streamlit.
FEEDBACK_REMINDER_CSS = """
<style>
:root { --rh-feedback-banner-height: 3.25rem; }
@media (max-width: 900px) {
    :root { --rh-feedback-banner-height: 4.5rem; }
}
@media (max-width: 480px) {
    :root { --rh-feedback-banner-height: 6rem; }
}
[data-testid="stAppViewContainer"] {
    top: var(--rh-feedback-banner-height);
}
/* Streamlit 1.57 gives this wrapper 100dvh, even when its viewport is
   shorter. Keep the scroll area and sticky chat input inside the reserved area. */
[data-testid="stAppViewContainer"] > div:has(> .stMain),
[data-testid="stAppViewContainer"] .stMain {
    height: 100%;
}
.rh-feedback-reminder {
    position: fixed;
    inset: 0 0 auto 0;
    z-index: 1000000;
    box-sizing: border-box;
    height: var(--rh-feedback-banner-height);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.5rem 1rem;
    background: #eef2ff;
    color: #003091;
    border-bottom: 1px solid #c7d2fe;
    font-size: 0.875rem;
    line-height: 1.5;
    text-align: center;
    overflow-wrap: anywhere;
}
</style>
"""


def configure_page(**kwargs) -> None:
    """Configure a page and render the same reminder on every page execution."""
    st.set_page_config(**kwargs)
    st.html(FEEDBACK_REMINDER_CSS + f'<aside class="rh-feedback-reminder" aria-label="Rappel d’évaluation">{FEEDBACK_REMINDER}</aside>')
