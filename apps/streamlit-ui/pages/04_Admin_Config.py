"""
🎛️ Admin Configuration Page - Runtime RAG parameter tuning

This page allows admins to modify RAG parameters and system prompts in real-time
without redeploying the application.

Protected by cookie-based group check (dgafpallianceadmin) with password fallback.
"""

from __future__ import annotations

import os

import streamlit as st
from assistant_rh_rag_pipeline.db_helpers import create_engine_from_env
from dotenv import load_dotenv

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PAGE CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

st.set_page_config(
    page_title="Admin Config",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded"  # Show sidebar for navigation
)

# Custom CSS
st.markdown("""
<style>
.config-section { 
    background: #f8f9fa; 
    border-radius: 8px; 
    padding: 1rem; 
    margin-bottom: 1rem;
    border: 1px solid #e9ecef;
}
.config-section h4 { 
    margin-top: 0; 
    color: #003091;
    border-bottom: 2px solid #003091;
    padding-bottom: 0.5rem;
}
</style>
""", unsafe_allow_html=True)

from src.ui.admin_auth import require_admin, show_admin_badge

require_admin()
show_admin_badge()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORTS (after auth)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from assistant_rh_rag_pipeline.admin import (
    add_acronym,
    delete_acronym,
    delete_prompt,
    duplicate_prompt,
    get_all_acronyms,
    get_all_prompts,
    get_categories,
    get_missing_acronyms,
    get_rag_config,
    init_acronyms_table,
    init_config_table,
    init_prompts_table,
    list_system_prompts,
    mark_acronym_as_added,
    reset_to_defaults,
    update_acronym,
    update_rag_config,
)
from assistant_rh_rag_pipeline.config import (
    PROMPT_TYPES,
    get_prompt_content,
    save_prompt,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEALTH CHECK FUNCTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_health_check_inline() -> dict:
    """Health check inline pour Admin Config."""
    import requests
    
    health = {}
    
    # Database
    try:
        engine = create_engine_from_env()
        if engine:
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            health["Database"] = {"status": "ok", "message": "PostgreSQL connecté"}
        else:
            health["Database"] = {"status": "warning", "message": "Pas de DB (mode CSV)"}
    except Exception as e:
        health["Database"] = {"status": "error", "message": str(e)[:50]}
    
    # Albert
    try:
        key = os.getenv("ALBERT_API_KEY")
        if key:
            r = requests.get("https://albert.api.etalab.gouv.fr/v1/models", 
                           headers={"Authorization": f"Bearer {key}"}, timeout=5)
            health["Albert"] = {"status": "ok" if r.status_code == 200 else "error", 
                               "message": "API OK" if r.status_code == 200 else f"HTTP {r.status_code}"}
        else:
            health["Albert"] = {"status": "warning", "message": "Clé non configurée"}
    except:
        health["Albert"] = {"status": "error", "message": "Timeout/Erreur"}
    
    # Scaleway
    try:
        key = os.getenv("SCALEWAY_API_KEY")
        url = os.getenv("SCALEWAY_BASE_URL", "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1")
        if key:
            r = requests.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
            health["Scaleway"] = {"status": "ok" if r.status_code == 200 else "error",
                                 "message": "API OK" if r.status_code == 200 else f"HTTP {r.status_code}"}
        else:
            health["Scaleway"] = {"status": "warning", "message": "Clé non configurée"}
    except:
        health["Scaleway"] = {"status": "error", "message": "Timeout/Erreur"}
    
    return health

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INITIALIZE TABLES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if "tables_initialized" not in st.session_state:
    init_config_table()
    init_prompts_table()
    st.session_state.tables_initialized = True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEADER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

st.title("⚙️ Configuration RAG - Admin")
st.markdown("Modifiez les paramètres du RAG en temps réel. **Les changements s'appliquent immédiatement à tous les utilisateurs.**")

# Load current config
config = get_rag_config()

# Status bar
col_status1, col_status2, col_status3 = st.columns(3)
with col_status1:
    st.metric("🕐 Dernière MAJ", config.updated_at[:16] if config.updated_at else "N/A")
with col_status2:
    st.metric("👤 Par", config.updated_by or "system")
with col_status3:
    if st.button("🔄 Rafraîchir la config", width="stretch", key="btn_refresh"):
        st.cache_data.clear()
        st.rerun()

st.divider()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TABS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

tab_config, tab_prompts, tab_acronyms = st.tabs(["🎛️ Paramètres RAG", "📝 System Prompts", "📚 Acronymes"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1: RAG PARAMETERS (All in one)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_config:
    changes = {}
    
    st.markdown("## 🆕 Configuration RAG V3")
    st.caption("Architecture avec dynamic chunking, LLM Selector, et triangulation des sources.")
    
    V3_MODELS = [
        ("openweight-large (GPT-OSS 120B)", "openweight-large"),
        ("openweight-medium (Mistral Small 24B)", "openweight-medium"),
        ("openweight-small (Ministral 8B)", "openweight-small"),
        ("openweight-code (Codestral 22B)", "openweight-code"),
        ("mistral-medium (Mistral Medium)", "mistral-medium-2508"),
    ]
    v3_model_display = [m[0] for m in V3_MODELS]
    v3_model_ids = [m[1] for m in V3_MODELS]
    
    col_v3_left, col_v3_right = st.columns(2)
    
    with col_v3_left:
        st.markdown("#### 🎯 Mode de Contexte")
        
        context_modes = ["narrow", "standard", "wide"]
        context_mode_labels = {
            "narrow": "🎯 Narrow - Réponse ciblée (moins de contexte)",
            "standard": "⚖️ Standard - Équilibré (défaut)",
            "wide": "🌐 Wide - Panorama complet (plus de contexte)"
        }
        current_context_mode = getattr(config, 'v3_context_mode', 'standard')
        new_context_mode = st.selectbox(
            "Mode de contexte",
            context_modes,
            index=context_modes.index(current_context_mode) if current_context_mode in context_modes else 1,
            format_func=lambda x: context_mode_labels.get(x, x),
            help="Narrow: questions précises. Standard: équilibré. Wide: procédures/panoramas.",
            key="select_v3_context_mode"
        )
        if new_context_mode != current_context_mode:
            changes["v3_context_mode"] = new_context_mode
        
        st.markdown("---")
        
        st.markdown("#### 🔍 Mode de Recherche")
        
        search_modes = ["semantic", "hybrid", "lexical"]
        search_mode_labels = {
            "semantic": "🧠 Semantic - Embeddings uniquement (recommandé)",
            "hybrid": "⚡ Hybrid - Semantic + Lexical (nécessite TSV indexé)",
            "lexical": "📝 Lexical - BM25/TSV uniquement (lent sans index)"
        }
        current_search_mode = getattr(config, 'v3_search_mode', 'semantic')
        new_search_mode = st.selectbox(
            "Mode de recherche",
            search_modes,
            index=search_modes.index(current_search_mode) if current_search_mode in search_modes else 0,
            format_func=lambda x: search_mode_labels.get(x, x),
            help="Semantic: rapide et précis. Hybrid: nécessite colonnes TSV pré-indexées.",
            key="select_v3_search_mode"
        )
        if new_search_mode != current_search_mode:
            changes["v3_search_mode"] = new_search_mode
        
        if new_search_mode in ["hybrid", "lexical"]:
            st.warning("⚠️ Lexical lent sans colonnes TSV pré-indexées. Préférez Semantic.")
        
        st.markdown("---")
        
        st.markdown("#### 💰 Budget de Tokens")
        
        current_token_budget = getattr(config, 'v3_token_budget', 8000)
        new_token_budget = st.slider(
            "Budget tokens contexte",
            min_value=4000, max_value=16000, value=current_token_budget, step=1000,
            help="Nombre max de tokens pour le contexte (hors prompt système)",
            key="slider_v3_token_budget"
        )
        if new_token_budget != current_token_budget:
            changes["v3_token_budget"] = new_token_budget
        
        st.caption("💡 Standard: 8000 | Wide: 12000")
        
        st.markdown("---")
        
        st.markdown("#### 📄 Seuils Doc Entier")
        
        current_doc_threshold = getattr(config, 'v3_doc_entire_threshold', 3500)
        new_doc_threshold = st.slider(
            "Seuil doc entier (standard)",
            min_value=1000, max_value=6000, value=current_doc_threshold, step=500,
            help="Inclure le doc entier si < ce seuil (tokens)",
            key="slider_v3_doc_threshold"
        )
        if new_doc_threshold != current_doc_threshold:
            changes["v3_doc_entire_threshold"] = new_doc_threshold
        
        st.caption("💡 Recommandé: 3500 (standard), 5000 (wide)")
    
    with col_v3_right:
        st.markdown("#### 🧠 LLM Selector V3")
        
        current_selector = getattr(config, 'v3_enable_selector', True)
        new_selector = st.toggle(
            "Activer LLM Selector",
            value=current_selector,
            help="Filtre et priorise les sections pertinentes via un LLM",
            key="toggle_v3_selector"
        )
        if new_selector != current_selector:
            changes["v3_enable_selector"] = new_selector
        
        if new_selector:
            current_v3_selector_model = getattr(config, 'v3_selector_model', 'openweight-large')
            current_v3_selector_idx = v3_model_ids.index(current_v3_selector_model) if current_v3_selector_model in v3_model_ids else 0
            
            new_v3_selector_model_display = st.selectbox(
                "Modèle Selector",
                v3_model_display,
                index=current_v3_selector_idx,
                help="Modèle pour la sélection de contexte",
                key="select_v3_selector_model"
            )
            new_v3_selector_model = v3_model_ids[v3_model_display.index(new_v3_selector_model_display)]
            if new_v3_selector_model != current_v3_selector_model:
                changes["v3_selector_model"] = new_v3_selector_model
        
        st.markdown("---")
        
        st.markdown("#### 🔺 Triangulation")
        
        current_triangulation = getattr(config, 'v3_triangulation_sections', 2)
        new_triangulation = st.slider(
            "Sections de triangulation",
            min_value=0, max_value=5, value=current_triangulation, step=1,
            help="Ajouter N sections d'autres sources pour diversité",
            key="slider_v3_triangulation"
        )
        if new_triangulation != current_triangulation:
            changes["v3_triangulation_sections"] = new_triangulation
        
        st.caption("💡 Recommandé: 2 (ajoute sections Service-Public si MATTE, et vice-versa)")
    
    st.divider()
    
    st.markdown("### 🔍 Retrieval V3")

    st.markdown("#### 📦 Sources de données")
    tables_available = ["matte", "service_public", "dgafp", "rgrh"]
    current_tables = getattr(config, 'v3_tables', tables_available)
    new_tables = st.multiselect(
        "Tables DE activées",
        tables_available,
        default=current_tables,
        help="Sources interrogées par le retrieval V3. DGAFP/Légifrance reste actif si sélectionné.",
        key="multiselect_v3_tables"
    )
    if set(new_tables) != set(current_tables):
        changes["v3_tables"] = new_tables

    active_sources = list(new_tables)
    st.caption(f"📊 Sources actives: {', '.join(active_sources)} ({len(active_sources)} tables)")

    col_ret1, col_ret2 = st.columns(2)
    
    with col_ret1:
        current_v3_top_k = getattr(config, 'v3_initial_top_k', 10)
        new_v3_top_k = st.slider(
            "Initial Top-K",
            min_value=5, max_value=30, value=current_v3_top_k, step=1,
            help="Nombre de chunks récupérés par source avant reranking",
            key="slider_v3_initial_top_k"
        )
        if new_v3_top_k != current_v3_top_k:
            changes["v3_initial_top_k"] = new_v3_top_k
        
        if new_search_mode == "hybrid":
            current_v3_alpha = getattr(config, 'v3_alpha', 0.5)
            new_v3_alpha = st.slider(
                "Alpha (Hybrid)",
                min_value=0.0, max_value=1.0, value=current_v3_alpha, step=0.1,
                help="0 = 100% lexical, 1 = 100% sémantique",
                key="slider_v3_alpha"
            )
            if new_v3_alpha != current_v3_alpha:
                changes["v3_alpha"] = new_v3_alpha
    
    with col_ret2:
        current_v3_reranker = getattr(config, 'v3_enable_reranker', True)
        new_v3_reranker = st.toggle(
            "Activer le Reranker (sections)",
            value=current_v3_reranker,
            help="Réordonne les sections agrégées avec un modèle de reranking (appliqué sur section_markdown)",
            key="toggle_v3_reranker"
        )
        if new_v3_reranker != current_v3_reranker:
            changes["v3_enable_reranker"] = new_v3_reranker
        
        if new_v3_reranker:
            current_v3_rerank_top_k = getattr(config, 'v3_rerank_top_k', 5)
            # max 15 -> 30: la config validée par l'eval (candidate_v2, run 115)
            # utilise 20 sections — le plafond 15 rendait la valeur mesurée
            # inatteignable depuis l'UI.
            new_v3_rerank_top_k = st.slider(
                "Sections après rerank",
                min_value=3, max_value=30, value=current_v3_rerank_top_k, step=1,
                help="Nombre de sections conservées après reranking",
                key="slider_v3_rerank_top_k"
            )
            if new_v3_rerank_top_k != current_v3_rerank_top_k:
                changes["v3_rerank_top_k"] = new_v3_rerank_top_k
            
            st.caption(f"📊 **Pipeline:** {new_v3_top_k} chunks → Agrégation sections → Reranker → **{new_v3_rerank_top_k} sections**")
        else:
            st.caption(f"📊 **Pipeline:** {new_v3_top_k} chunks → Agrégation sections (sans rerank)")
    
    st.divider()
    
    st.markdown("### 🤖 Génération LLM (V3)")
    
    col_gen1, col_gen2 = st.columns(2)
    
    with col_gen1:
        current_v3_gen_model = getattr(config, 'v3_generator_model', 'openweight-large')
        current_v3_gen_idx = v3_model_ids.index(current_v3_gen_model) if current_v3_gen_model in v3_model_ids else 0
        
        new_v3_gen_model_display = st.selectbox(
            "Modèle Générateur",
            v3_model_display,
            index=current_v3_gen_idx,
            help="Modèle utilisé pour générer les réponses finales",
            key="select_v3_generator_model"
        )
        new_v3_gen_model = v3_model_ids[v3_model_display.index(new_v3_gen_model_display)]
        if new_v3_gen_model != current_v3_gen_model:
            changes["v3_generator_model"] = new_v3_gen_model
        
        st.caption("🇫🇷 Tous les modèles via Albert API (DINUM)")
    
    with col_gen2:
        new_v3_temperature = st.slider(
            "🌡️ Température",
            min_value=0.0, max_value=1.5, value=getattr(config, 'v3_temperature', 0.0), step=0.1,
            help="0 = déterministe, >0.7 = créatif",
            key="slider_temperature_v3"
        )
        if new_v3_temperature != getattr(config, 'v3_temperature', 0.0):
            changes["v3_temperature"] = new_v3_temperature
        
        available_prompts = list_system_prompts()
        if available_prompts:
            current_v3_prompt = getattr(config, 'v3_system_prompt_name', 'system_prompt_V6_optimized.md')
            current_v3_prompt_idx = available_prompts.index(current_v3_prompt) if current_v3_prompt in available_prompts else 0
            new_v3_system_prompt = st.selectbox(
                "📝 System Prompt",
                available_prompts,
                index=current_v3_prompt_idx,
                help="Prompt système pour guider les réponses",
                key="select_system_prompt_v3"
            )
            if new_v3_system_prompt != current_v3_prompt:
                changes["v3_system_prompt_name"] = new_v3_system_prompt
    
    st.divider()
    
    st.markdown("### 📝 Prompts par Agent (V3)")
    st.caption("Sélectionnez le prompt utilisé pour chaque étape du pipeline.")
    
    col_p1, col_p2, col_p3 = st.columns(3)
    
    with col_p1:
        st.markdown("**🧠 Intent Classifier**")
        intent_prompts = list_system_prompts("intent_gating")
        if intent_prompts:
            current_intent_prompt = getattr(config, 'v3_intent_prompt_name', 'intent_unified.md')
            if current_intent_prompt not in intent_prompts:
                intent_prompts.insert(0, current_intent_prompt)
            intent_idx = intent_prompts.index(current_intent_prompt) if current_intent_prompt in intent_prompts else 0
            new_intent_prompt = st.selectbox(
                "Prompt Intent",
                intent_prompts,
                index=intent_idx,
                help="Classification d'intention + thème + reformulation",
                key="select_v3_intent_prompt"
            )
            if new_intent_prompt != current_intent_prompt:
                changes["v3_intent_prompt_name"] = new_intent_prompt
        else:
            st.info("intent_unified.md (default)")
    
    with col_p2:
        st.markdown("**🎯 LLM Selector**")
        selector_prompts = list_system_prompts("llm_selector")
        if selector_prompts:
            current_selector_prompt = getattr(config, 'v3_selector_prompt_name', 'v3_selector_business.md')
            if current_selector_prompt not in selector_prompts:
                selector_prompts.insert(0, current_selector_prompt)
            selector_idx = selector_prompts.index(current_selector_prompt) if current_selector_prompt in selector_prompts else 0
            new_selector_prompt = st.selectbox(
                "Prompt Selector",
                selector_prompts,
                index=selector_idx,
                help="Sélection des passages pertinents",
                key="select_v3_selector_prompt"
            )
            if new_selector_prompt != current_selector_prompt:
                changes["v3_selector_prompt_name"] = new_selector_prompt
        else:
            st.info("v3_selector_business.md (default)")
    
    with col_p3:
        st.markdown("**💬 Generator**")
        st.caption("(Configuré ci-dessus)")
        st.info(f"📄 {getattr(config, 'v3_system_prompt_name', 'system_prompt_V6_optimized.md')}")
    
    st.divider()
    
    st.markdown("### 🔧 Debug V3")
    col_debug1, col_debug2 = st.columns(2)
    
    with col_debug1:
        new_verbose = st.toggle(
            "📋 Mode Verbose",
            value=config.verbose_mode,
            help="Affiche les logs détaillés",
            key="toggle_verbose_v3"
        )
        if new_verbose != config.verbose_mode:
            changes["verbose_mode"] = new_verbose
    
    # ─────────────────────────────────────────────────────────────────────────
    # SAVE / RESET BUTTONS
    # ─────────────────────────────────────────────────────────────────────────
    st.divider()
    
    # Show pending changes
    if changes:
        st.info(f"📝 **{len(changes)} modification(s) en attente:** {', '.join(changes.keys())}")
    
    col_save, col_reset = st.columns([1, 1])
    
    with col_save:
        if st.button("💾 Sauvegarder", type="primary", width="stretch", disabled=len(changes) == 0, key="btn_save_config"):
            success, errors = update_rag_config(updated_by="admin", **changes)
            if success:
                st.success(f"✅ {len(changes)} paramètre(s) mis à jour !")
                st.balloons()
                st.cache_data.clear()
                st.rerun()
            else:
                for field, error in errors.items():
                    st.error(f"❌ {field}: {error}")
    
    with col_reset:
        if st.button("🔄 Reset défauts", width="stretch", key="btn_reset_config"):
            if reset_to_defaults(updated_by="admin"):
                st.success("✅ Configuration réinitialisée")
                st.cache_data.clear()
                st.rerun()
    

    
    # Current config JSON
    with st.expander("📋 Configuration actuelle (JSON)", expanded=False):
        st.json(config.to_dict())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2: SYSTEM PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_prompts:
    st.markdown("### 📝 Gestion des System Prompts")
    st.markdown("Créez, modifiez ou supprimez les prompts système. Le prompt actif est défini dans l'onglet Paramètres.")
    
    # ─────────────────────────────────────────────────────────────────────────
    # PROMPT TYPE SELECTOR
    # ─────────────────────────────────────────────────────────────────────────
    
    prompt_type_options = list(PROMPT_TYPES.keys())
    prompt_type_labels = list(PROMPT_TYPES.values())
    
    # Initialize selected type in session state
    if "selected_prompt_type" not in st.session_state:
        st.session_state.selected_prompt_type = "generator"
    
    selected_type_label = st.radio(
        "Type de prompt",
        prompt_type_labels,
        index=prompt_type_options.index(st.session_state.selected_prompt_type),
        horizontal=True,
        key="radio_prompt_type"
    )
    selected_prompt_type = prompt_type_options[prompt_type_labels.index(selected_type_label)]
    
    # Update session state if type changed
    if selected_prompt_type != st.session_state.selected_prompt_type:
        st.session_state.selected_prompt_type = selected_prompt_type
        st.session_state.selected_prompt = None  # Reset selected prompt
        st.rerun()
    
    st.divider()
    
    # Load prompts filtered by type (descending order)
    all_prompts = get_all_prompts(selected_prompt_type)
    prompt_names = [p.name for p in all_prompts] if all_prompts else list_system_prompts(selected_prompt_type)
    prompt_names = sorted(prompt_names, reverse=True)

    # Determine which prompt is active for the selected type
    _active_prompt_by_type = {
        "generator": getattr(config, 'v3_system_prompt_name', ''),
        "llm_selector": getattr(config, 'v3_selector_prompt_name', ''),
        "intent_gating": getattr(config, 'v3_intent_prompt_name', ''),
    }
    _active_prompt_name = _active_prompt_by_type.get(selected_prompt_type, '')
    
    # Two columns: list + editor
    col_list, col_editor = st.columns([1, 2])
    
    with col_list:
        st.markdown("#### 📚 Prompts disponibles")
        
        # Initialize selected prompt
        if "selected_prompt" not in st.session_state:
            st.session_state.selected_prompt = prompt_names[0] if prompt_names else None
        
        # List prompts as buttons
        for idx, prompt_name in enumerate(prompt_names):
            is_active = prompt_name == _active_prompt_name
            label = f"{'✅ ' if is_active else ''}{prompt_name}"
            if st.button(label, key=f"btn_prompt_{idx}_{prompt_name}", width="stretch"):
                st.session_state.selected_prompt = prompt_name
                st.rerun()
        
        st.divider()
        
        # Create new prompt button
        if st.button("➕ Nouveau prompt", width="stretch", type="primary", key="btn_new_prompt"):
            st.session_state.selected_prompt = "__new__"
            st.rerun()
    
    with col_editor:
        selected = st.session_state.get("selected_prompt")
        
        if selected == "__new__":
            # New prompt form
            st.markdown(f"#### ✨ Créer un nouveau prompt ({PROMPT_TYPES[selected_prompt_type]})")
            
            new_name = st.text_input("Nom du prompt", placeholder="mon_prompt.md", key="input_new_prompt_name")
            new_description = st.text_input("Description", placeholder="Description courte du prompt", key="input_new_prompt_desc")
            new_content = st.text_area(
                "Contenu",
                height=400,
                placeholder="Écrivez votre prompt ici...\n\nVous pouvez utiliser du Markdown.\n\nVariables disponibles:\n- {query} = Question utilisateur\n- {documents} = Documents disponibles\n- {target_chunks} = Nombre de chunks cible",
                key="textarea_new_prompt_content"
            )
            
            col_create, col_cancel = st.columns(2)
            with col_create:
                if st.button("💾 Créer", type="primary", width="stretch", key="btn_create_prompt"):
                    if new_name and new_content:
                        # Normalize name to .md extension
                        normalized_name = new_name if new_name.endswith(".md") else f"{new_name}.md"
                        success = save_prompt(
                            name=normalized_name,
                            content=new_content,
                            prompt_type=selected_prompt_type,
                            description=new_description,
                            updated_by="admin"
                        )
                        if success:
                            st.success("✅ Prompt créé avec succès")
                            st.session_state.selected_prompt = normalized_name
                            st.rerun()
                        else:
                            st.error("❌ Erreur lors de la création du prompt")
                    else:
                        st.warning("Nom et contenu requis")
            
            with col_cancel:
                if st.button("❌ Annuler", width="stretch", key="btn_cancel_new_prompt"):
                    st.session_state.selected_prompt = prompt_names[0] if prompt_names else None
                    st.rerun()
        
        elif selected:
            # Edit existing prompt
            st.markdown(f"#### 📄 {selected}")
            
            # Get prompt content
            content = get_prompt_content(selected)
            
            if content:
                # Find prompt metadata
                prompt_meta = next((p for p in all_prompts if p.name == selected), None)
                
                if prompt_meta:
                    st.caption(f"📅 Modifié le {prompt_meta.updated_at[:16] if prompt_meta.updated_at else 'N/A'} par {prompt_meta.updated_by}")
                
                # Description
                current_desc = prompt_meta.description if prompt_meta else ""
                new_description = st.text_input("Description", value=current_desc, key=f"input_desc_{selected}")
                
                # Content editor
                edited_content = st.text_area(
                    "Contenu",
                    value=content,
                    height=400,
                    key=f"textarea_content_{selected}"
                )
                
                # Action buttons
                col_save_prompt, col_duplicate, col_delete = st.columns(3)
                
                with col_save_prompt:
                    if st.button("💾 Sauvegarder", type="primary", width="stretch", key=f"btn_save_prompt_{selected}"):
                        if edited_content != content or new_description != current_desc:
                            success = save_prompt(
                                name=selected,
                                content=edited_content,
                                prompt_type=selected_prompt_type,
                                description=new_description,
                                updated_by="admin"
                            )
                            if success:
                                st.success("✅ Prompt sauvegardé avec succès")
                                st.rerun()
                            else:
                                st.error("❌ Erreur lors de la sauvegarde du prompt")
                        else:
                            st.info("Aucune modification")
                
                with col_duplicate:
                    if st.button("📋 Dupliquer", width="stretch", key=f"btn_dup_prompt_{selected}"):
                        new_name = f"{selected.replace('.md', '')}_copy.md"
                        success, msg = duplicate_prompt(selected, new_name, "admin")
                        if success:
                            st.success(msg)
                            st.session_state.selected_prompt = new_name
                            st.rerun()
                        else:
                            st.error(msg)
                
                with col_delete:
                    if selected != "system_prompt.md":  # Can't delete default
                        if st.button("🗑️ Supprimer", width="stretch", key=f"btn_del_prompt_{selected}"):
                            success, msg = delete_prompt(selected)
                            if success:
                                st.success(msg)
                                st.session_state.selected_prompt = prompt_names[0] if prompt_names else None
                                st.rerun()
                            else:
                                st.error(msg)
                    else:
                        st.button("🗑️ Supprimer", width="stretch", disabled=True, help="Impossible de supprimer le prompt par défaut", key="btn_del_default_disabled")
                
                # Preview
                with st.expander("👁️ Aperçu (rendu Markdown)", expanded=False):
                    st.markdown(edited_content)
            else:
                st.warning(f"Prompt '{selected}' introuvable")
        else:
            st.info("Sélectionnez un prompt dans la liste ou créez-en un nouveau")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 3: ACRONYMS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_acronyms:
    st.markdown("### 📚 Dictionnaire d'acronymes")
    st.caption("Gérez les acronymes utilisés pour l'expansion automatique des requêtes. Les modifications sont appliquées en temps réel.")
    
    # Initialize table if needed
    init_acronyms_table()
    
    # Load acronyms
    acronyms = get_all_acronyms()
    categories = get_categories()
    
    col_list, col_form = st.columns([2, 1])
    
    with col_list:
        st.markdown("#### Liste des acronymes")
        
        # Search/filter
        search_query = st.text_input("🔍 Rechercher", placeholder="Tapez un acronyme ou son expansion...", key="acronym_search")
        
        # Filter acronyms
        if search_query:
            search_lower = search_query.lower()
            filtered_acronyms = [
                a for a in acronyms
                if search_lower in a.acronym.lower() or search_lower in a.expansion.lower()
            ]
        else:
            filtered_acronyms = acronyms
        
        # Display as dataframe
        if filtered_acronyms:
            import pandas as pd
            df = pd.DataFrame([
                {
                    "Acronyme": a.acronym,
                    "Expansion": a.expansion,
                    "Catégorie": a.category,
                }
                for a in filtered_acronyms
            ])
            
            # Editable dataframe
            edited_df = st.data_editor(
                df,
                hide_index=True,
                width="stretch",
                num_rows="fixed",  # Don't allow adding rows here
                column_config={
                    "Acronyme": st.column_config.TextColumn("Acronyme", width="small", disabled=True),
                    "Expansion": st.column_config.TextColumn("Expansion", width="large"),
                    "Catégorie": st.column_config.SelectboxColumn("Catégorie", options=categories + ["autre"], width="small"),
                },
                key="acronym_editor"
            )
            
            # Detect changes and update
            if not df.equals(edited_df):
                for idx, row in edited_df.iterrows():
                    original = df.iloc[idx]
                    if row["Expansion"] != original["Expansion"] or row["Catégorie"] != original["Catégorie"]:
                        update_acronym(
                            acronym=row["Acronyme"],
                            expansion=row["Expansion"],
                            category=row["Catégorie"]
                        )
                st.success("✅ Modifications enregistrées")
                st.rerun()
            
            st.caption(f"📊 {len(filtered_acronyms)} acronymes affichés sur {len(acronyms)} au total")
        else:
            st.info("Aucun acronyme trouvé")
    
    with col_form:
        st.markdown("#### ➕ Ajouter un acronyme")
        
        with st.form("add_acronym_form", clear_on_submit=True):
            new_acronym = st.text_input("Acronyme", placeholder="Ex: CDD", max_chars=20)
            new_expansion = st.text_input("Expansion", placeholder="Ex: Contrat à Durée Déterminée")
            
            submitted = st.form_submit_button("➕ Ajouter", type="primary", width="stretch")
            
            if submitted:
                if not new_acronym or not new_expansion:
                    st.error("❌ L'acronyme et l'expansion sont requis")
                else:
                    success = add_acronym(
                        acronym=new_acronym.upper().strip(),
                        expansion=new_expansion.strip(),
                        category="général",  # Catégorie par défaut
                    )
                    if success:
                        st.success(f"✅ Acronyme '{new_acronym.upper()}' ajouté")
                        st.rerun()
                    else:
                        st.error("❌ Erreur lors de l'ajout (acronyme peut-être déjà existant)")
        
        st.markdown("---")
        st.markdown("#### 🗑️ Supprimer un acronyme")
        
        acronym_to_delete = st.selectbox(
            "Sélectionner l'acronyme à supprimer",
            options=[a.acronym for a in acronyms],
            key="acronym_delete_select"
        )
        
        if st.button("🗑️ Supprimer", type="secondary", width="stretch", key="btn_delete_acronym"):
            if acronym_to_delete:
                success = delete_acronym(acronym_to_delete)
                if success:
                    st.success(f"✅ Acronyme '{acronym_to_delete}' supprimé")
                    st.rerun()
                else:
                    st.error("❌ Erreur lors de la suppression")
    
    # ─────────────────────────────────────────────────────────────────────────
    # MISSING ACRONYMS SECTION
    # ─────────────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🔍 Acronymes manquants détectés")
    st.caption("Acronymes détectés dans les requêtes utilisateur mais absents du dictionnaire. Utilisez le formulaire ci-dessus pour les ajouter, ou marquez-les comme traités.")
    
    # Load missing acronyms
    missing_acronyms = get_missing_acronyms(limit=30, min_occurrences=1)
    
    if missing_acronyms:
        import pandas as pd
        
        # Separate pending and treated
        missing_pending = [m for m in missing_acronyms if not m.get("added_to_acronyms", False)]
        missing_treated = [m for m in missing_acronyms if m.get("added_to_acronyms", False)]
        
        if missing_pending:
            # Display pending acronyms
            df_missing = pd.DataFrame([
                {
                    "Acronyme": m["acronym"],
                    "Occ.": m["occurrence_count"],
                    "Vu le": m["last_seen_at"][:10] if m.get("last_seen_at") else "-",
                    "Exemple de requête": (m.get("sample_query", "")[:] + "...") if m.get("sample_query") and len(m.get("sample_query", "")) > 60 else m.get("sample_query", "-"),
                }
                for m in missing_pending
            ])
            
            st.dataframe(
                df_missing,
                hide_index=True,
                width="stretch",
                column_config={
                    "Acronyme": st.column_config.TextColumn("Acronyme", width="small"),
                    "Occ.": st.column_config.NumberColumn("Occ.", width="small"),
                    "Vu le": st.column_config.TextColumn("Vu le", width="small"),
                    "Exemple de requête": st.column_config.TextColumn("Exemple de requête", width="large"),
                },
            )
            
            # Simple action: mark as treated (ignored)
            col_select, col_action = st.columns([2, 1])
            
            with col_select:
                selected_to_ignore = st.selectbox(
                    "Acronyme à ignorer",
                    options=[m["acronym"] for m in missing_pending],
                    key="select_acronym_to_ignore",
                    help="Sélectionnez un acronyme non pertinent à marquer comme traité",
                )
            
            with col_action:
                if st.button("✅ Marquer comme traité", width="stretch", key="btn_ignore_acronym"):
                    mark_acronym_as_added(selected_to_ignore)
                    st.success(f"✅ '{selected_to_ignore}' marqué comme traité")
                    st.rerun()
            
            st.caption(f"⏳ {len(missing_pending)} acronyme(s) en attente • ✅ {len(missing_treated)} traité(s)")
        else:
            st.success("✅ Tous les acronymes détectés ont été traités !")
            if missing_treated:
                st.caption(f"📊 {len(missing_treated)} acronyme(s) traité(s) au total")
    else:
        st.info("🎉 Aucun acronyme manquant détecté dans les requêtes récentes")
    
    # Stats
    st.markdown("---")
    st.markdown("#### 📊 Statistiques")
    
    col_stat1, col_stat2, col_stat3 = st.columns(3)
    with col_stat1:
        st.metric("Total acronymes", len(acronyms))
    with col_stat2:
        st.metric("Catégories", len(set(a.category for a in acronyms)))
    with col_stat3:
        # Most recent addition
        if acronyms:
            sorted_by_date = sorted([a for a in acronyms if a.created_at], key=lambda x: x.created_at, reverse=True)
            if sorted_by_date:
                st.metric("Dernier ajout", sorted_by_date[0].acronym)
            else:
                st.metric("Dernier ajout", "-")
        else:
            st.metric("Dernier ajout", "-")
