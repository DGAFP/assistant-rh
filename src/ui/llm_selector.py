"""
Streamlit UI components for LLM configuration.
"""
import os
from typing import Any, Dict

import streamlit as st


def llm_endpoint_selector(
    default_provider: str = "albert",
    default_model: str = "albert-large",
    default_temperature: float = 0.0,
    show_temperature: bool = True,
    key_prefix: str = "",
) -> Dict[str, Any]:
    """
    Streamlit widget for selecting LLM endpoint configuration.
    
    Returns a dict with:
        - provider: "albert", "scaleway", or "openai"
        - model: selected model name
        - temperature: float
        - base_url: optional base URL for compatible providers
        - api_key_env: name of the environment variable for API key
    
    Args:
        default_provider: "albert", "scaleway", or "openai"
        default_model: default model name
        default_temperature: default temperature (0.0-1.0)
        show_temperature: whether to show temperature slider
        key_prefix: unique prefix for st.session_state keys (useful when multiple selectors)
    
    Example:
        ```python
        llm_config = llm_endpoint_selector()
        
        from assistant_rh_rag_pipeline.llm_client import ChatLLM
        llm = ChatLLM(
            provider=llm_config["provider"],
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            base_url=llm_config.get("base_url"),
            api_key_env=llm_config.get("api_key_env"),
        )
        ```
    """
    # st.markdown("#### LLM Endpoint")
    
    # Provider selection (Albert primary, Scaleway fallback)
    providers = ["Albert", "Scaleway"]
    if default_provider == "scaleway":
        provider_idx = 1
    else:
        provider_idx = 0
    
    llm_provider = st.selectbox(
        "LLM Provider",
        providers,
        index=provider_idx,
        key=f"{key_prefix}llm_provider"
    )
    
    # Helper function to list models from OpenAI-compatible endpoint
    def list_models_openai_compatible(base_url: str, api_key: str):
        """List models from an OpenAI-compatible API endpoint."""
        import requests
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            # OpenAI-compatible typically returns {"data":[{"id":"..."}]}
            return sorted([m.get("id") for m in data.get("data", []) if m.get("id")])
        except Exception as e:
            st.warning(f"Impossible de lister les modèles sur {base_url} : {e}")
            return []
    
    # Configure based on provider
    gen_model = default_model
    llm_base_url = None
    llm_api_key_env = None
    
    if llm_provider.lower().startswith("scaleway"):
        # Scaleway provider (OpenAI-compatible)
        llm_api_key_env = "SCALEWAY_API_KEY"
        
        # Base URL from environment (no UI input needed)
        llm_base_url = os.getenv("SCALEWAY_BASE_URL", "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1")
        
        # Model selection for Scaleway - Liste des modèles LLM disponibles (vérifié via API)
        # Format: (display_name, model_id)
        scaleway_models_display = [
            ("Qwen3 235B (recommandé)", "qwen3-235b-a22b-instruct-2507"),
            ("Qwen3 Coder 30B", "qwen3-coder-30b-a3b-instruct"),
            ("GPT-OSS 120B", "gpt-oss-120b"),
            ("Llama 3.3 70B", "llama-3.3-70b-instruct"),
            ("DeepSeek R1 Distill 70B", "deepseek-r1-distill-llama-70b"),
            ("Gemma 3 27B", "gemma-3-27b-it"),
            ("Mistral Nemo 12B", "mistral-nemo-instruct-2407"),
        ]
        
        scaleway_display_names = [m[0] for m in scaleway_models_display]
        scaleway_model_ids = [m[1] for m in scaleway_models_display]
        
        # Find default index
        default_scw_idx = 0
        if default_model in scaleway_model_ids:
            default_scw_idx = scaleway_model_ids.index(default_model)
        
        selected_display = st.selectbox(
            "Modèle Scaleway",
            scaleway_display_names,
            index=default_scw_idx,
            key=f"{key_prefix}scaleway_model_select"
        )
        
        # Map display name back to model ID
        gen_model = scaleway_model_ids[scaleway_display_names.index(selected_display)]
        
    else:
        # Albert (OpenAI-compatible)
        llm_api_key_env = "ALBERT_API_KEY"
        
        # Base URL from environment (no UI input needed)
        llm_base_url = os.getenv("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1")
        
        # Modèles Albert avec alias et vrais noms
        # Format: (display_name, model_id)
        albert_models_display = [
            ("mistral-medium-2508 (Mistral Medium)", "mistral-medium-2508"),
            ("openweight-large (GPT-OSS 120B)", "openweight-large"),
            ("albert-large (Mistral-Small 24B)", "albert-large"),
            ("openweight-medium (Mistral-Small 24B - équiv. albert-large)", "openweight-medium"),
            ("openweight-code (Qwen3-Coder 30B)", "openweight-code"),
            ("albert-code (Qwen2.5-Coder 32B)", "albert-code"),
            ("albert-small (Llama-3.1 8B)", "albert-small"),
            ("openweight-small (Ministral 8B)", "openweight-small"),
        ]
        
        albert_display_names = [m[0] for m in albert_models_display]
        albert_model_ids = [m[1] for m in albert_models_display]
        
        # Find default index
        default_albert_idx = 0
        if default_model in albert_model_ids:
            default_albert_idx = albert_model_ids.index(default_model)
        
        selected_display = st.selectbox(
            "Modèle Albert",
            albert_display_names,
            index=default_albert_idx,
            key=f"{key_prefix}albert_model_select"
        )
        
        # Map display name back to model ID
        gen_model = albert_model_ids[albert_display_names.index(selected_display)]
    
    # Temperature slider (after model selection)
    llm_temperature = default_temperature
    if show_temperature:
        llm_temperature = st.slider(
            "Température",
            0.0, 1.0, default_temperature, 0.1,
            key=f"{key_prefix}llm_temperature"
        )
    
    # Return configuration dict
    provider_lower = llm_provider.lower()
    if provider_lower.startswith("scaleway"):
        provider_name = "scaleway"
    else:
        provider_name = "albert"
    
    return {
        "provider": provider_name,
        "model": gen_model,
        "temperature": llm_temperature,
        "base_url": llm_base_url,
        "api_key_env": llm_api_key_env,
    }

