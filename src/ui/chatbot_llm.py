"""
Chatbot LLM - Client LLM et fonctions de streaming.

Extrait de 01_Chatbot.py pour plus de lisibilité.
"""

import os
import re
import time
from typing import Any, Generator, List

import streamlit as st
from assistant_rh_rag_pipeline.llm_client import ChatLLM

# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner="🤖 Initialisation du modèle LLM...")
def get_llm_client(
    provider: str,
    model: str, 
    temperature: float,
    system_prompt_text: str,
    prompt_name: str,
    base_url: str = None,
    api_key_env: str = None,
) -> ChatLLM:
    """
    Create and cache LLM client based on configuration.
    
    Le cache se réinitialise automatiquement quand:
    - Le provider/model/temperature change
    - Le system_prompt_text change
    - Le prompt_name change
    """
    print(f"🔄 Initialisation LLM: {provider}/{model} avec prompt '{prompt_name}'")
    return ChatLLM(
        provider=provider,
        model=model,
        temperature=temperature,
        base_url=base_url,
        api_key_env=api_key_env,
        system_prompt=system_prompt_text
    )


# ═══════════════════════════════════════════════════════════════════════════════
# FALLBACK CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

def get_fallback_config(temperature: float = 0.0) -> dict:
    """Return Scaleway configuration as fallback when Albert is down."""
    return {
        "provider": "scaleway",
        "model": "qwen3-235b-a22b-instruct-2507",
        "temperature": temperature,
        "base_url": os.getenv("SCALEWAY_BASE_URL", "https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1"),
        "api_key_env": "SCALEWAY_API_KEY",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STREAMING WITH FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

def stream_with_fallback(
    llm_client: ChatLLM, 
    prompt: str, 
    system_prompt: str,
    llm_config: dict,
) -> Generator[str, None, None]:
    """
    Stream LLM response with automatic fallback to Scaleway if Albert fails.
    
    Args:
        llm_client: Primary LLM client (typically Albert)
        prompt: User prompt
        system_prompt: System prompt for the LLM
        llm_config: Dict with provider, model, temperature, etc.
        
    Yields:
        str: Response chunks
        
    Side effects:
        Sets st.session_state['actual_llm_provider'] and st.session_state['actual_llm_model']
        Sets st.session_state['ttft_ms'] for Time to First Token.
        Sets st.session_state['total_chars'] for throughput calculation.
    """
    t_stream_start = time.time()
    first_token_received = False
    total_chars = 0
    
    try:
        # Track actual LLM used (primary)
        st.session_state['actual_llm_provider'] = llm_config['provider']
        st.session_state['actual_llm_model'] = llm_config['model']
        st.session_state['used_fallback'] = False
        
        # Try with primary LLM
        for chunk in llm_client.chat_stream(prompt):
            if not first_token_received:
                ttft_ms = (time.time() - t_stream_start) * 1000
                st.session_state['ttft_ms'] = ttft_ms
                first_token_received = True
            
            total_chars += len(chunk)
            yield chunk
        
        st.session_state['total_chars'] = total_chars
        
    except Exception as e:
        print(f"⚠️ LLM Fallback: {llm_config['provider']} indisponible ({e}), basculement vers Scaleway...")
        
        # Reset metrics for fallback
        t_stream_start = time.time()
        first_token_received = False
        total_chars = 0
        
        if not os.getenv("SCALEWAY_API_KEY"):
            yield "❌ Service temporairement indisponible. Veuillez réessayer dans quelques instants."
            return
        
        try:
            fallback_config = get_fallback_config(llm_config.get("temperature", 0.0))
            
            # Track actual LLM used (fallback)
            st.session_state['actual_llm_provider'] = fallback_config['provider']
            st.session_state['actual_llm_model'] = fallback_config['model']
            st.session_state['used_fallback'] = True
            
            fallback_llm = ChatLLM(
                provider=fallback_config["provider"],
                model=fallback_config["model"],
                temperature=fallback_config["temperature"],
                base_url=fallback_config["base_url"],
                api_key_env=fallback_config["api_key_env"],
                system_prompt=system_prompt
            )
            
            for chunk in fallback_llm.chat_stream(prompt):
                if not first_token_received:
                    ttft_ms = (time.time() - t_stream_start) * 1000
                    st.session_state['ttft_ms'] = ttft_ms
                    first_token_received = True
                
                total_chars += len(chunk)
                yield chunk
            
            st.session_state['total_chars'] = total_chars
            
        except Exception as fallback_error:
            print(f"❌ LLM Fallback failed: {fallback_error}")
            yield "❌ Service temporairement indisponible. Veuillez réessayer dans quelques instants."


def stream_and_filter_sources(
    llm_client: ChatLLM, 
    prompt: str, 
    system_prompt: str,
    llm_config: dict,
    rag_config: Any = None,
) -> Generator[str, None, None]:
    """
    Stream la réponse du LLM en filtrant la ligne SOURCES: X, Y, Z en temps réel.
    
    Masque la ligne SOURCES pour qu'elle ne soit jamais visible par l'utilisateur,
    tout en la capturant pour filtrer les sources affichées.
    
    Args:
        llm_client: LLM client
        prompt: User prompt
        system_prompt: System prompt
        llm_config: Dict with provider, model, temperature, etc.
        rag_config: RAG config (optional, for prompt name)
        
    Yields:
        str: Response chunks (sans la ligne SOURCES)
        
    Returns via session_state:
        Stocke la ligne SOURCES parsée dans st.session_state['last_sources_line']
    """
    # Re-initialize LLM if needed
    if llm_client is None:
        try:
            llm_client = get_llm_client(
                provider=llm_config["provider"],
                model=llm_config["model"],
                temperature=llm_config["temperature"],
                system_prompt_text=system_prompt,
                prompt_name=rag_config.system_prompt_name if rag_config else "default",
                base_url=llm_config.get("base_url"),
                api_key_env=llm_config.get("api_key_env"),
            )
        except Exception as e:
            yield f"❌ Impossible d'initialiser le modèle LLM : {e}"
            return
    
    full_answer = ""
    buffer = ""
    sources_started = False
    TRIGGER = "SOURCES:"
    
    for chunk in stream_with_fallback(llm_client, prompt, system_prompt, llm_config):
        if sources_started:
            st.session_state['last_sources_line'] += chunk
            full_answer += chunk
        else:
            buffer += chunk
            full_answer += chunk
            
            if TRIGGER in buffer:
                sources_started = True
                parts = buffer.split(TRIGGER, 1)
                clean_part = parts[0].rstrip()
                sources_part = TRIGGER + parts[1]
                
                # Nettoyer les backticks orphelins
                clean_part = re.sub(r'\n*```\s*$', '', clean_part)
                clean_part = re.sub(r'\n*`+\s*$', '', clean_part)
                clean_part = clean_part.rstrip()
                
                while clean_part.endswith("```"):
                    clean_part = clean_part[:-3].rstrip()
                while clean_part.endswith("`"):
                    clean_part = clean_part[:-1].rstrip()
                
                if clean_part:
                    yield clean_part
                
                st.session_state['last_sources_line'] = sources_part
                buffer = ""
            else:
                safe_length = len(buffer) - len(TRIGGER) + 1
                
                if safe_length > 0:
                    to_yield = buffer[:safe_length]
                    yield to_yield
                    buffer = buffer[safe_length:]
    
    # Fin du stream
    if not sources_started and buffer:
        buffer = buffer.rstrip()
        buffer = re.sub(r'\n*```\s*$', '', buffer)
        buffer = re.sub(r'\n*`+\s*$', '', buffer)
        buffer = buffer.rstrip()
        while buffer.endswith("```"):
            buffer = buffer[:-3].rstrip()
        while buffer.endswith("`"):
            buffer = buffer[:-1].rstrip()
        if buffer:
            yield buffer
    
    if not sources_started:
        st.session_state['last_sources_line'] = None


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCES PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_sources_line(sources_line: str) -> List[int]:
    """
    Parse la ligne SOURCES avec support de multiples formats.
    
    Formats acceptés :
    - "SOURCES: 1, 3, 5"
    - "sources: 1,3,5"
    - "Source: 1 3 5"
    - "Sources utilisées: 1, 2"
    - "[1, 3, 5]"
    
    Args:
        sources_line: Ligne contenant les numéros de sources
        
    Returns:
        Liste des index (ex: [1, 3, 5]) ou [] si parsing échoue
    """
    if not sources_line:
        return []
    
    try:
        patterns = [
            r"SOURCES?\s*:\s*([\d,\s]+)",
            r"Sources?\s+utilisées?\s*:\s*([\d,\s]+)",
            r"\[(\d+(?:,\s*\d+)*)\]",
            r"(\d+(?:\s*,\s*\d+)+)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, sources_line, re.IGNORECASE)
            if match:
                numbers = re.findall(r'\d+', match.group(1) if match.lastindex else match.group(0))
                if numbers:
                    return [int(n) for n in numbers]
        
        return []
        
    except Exception:
        return []

