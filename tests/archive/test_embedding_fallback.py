#!/usr/bin/env python3
"""
🧪 Test du système de fallback Embeddings complet.

Ce script teste:
1. FallbackEmbedder: Albert → Scaleway BGE
2. MultiSourcePGRetriever: sélection dynamique de colonne
3. Flux complet: embedding + retrieval avec différents modèles

Usage:
    python tests/test_embedding_fallback.py
    
    # Avec tunnel Scalingo actif:
    python tests/test_embedding_fallback.py --with-db
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST UTILITIES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def print_result(test_name: str, passed: bool, details: str = ""):
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status} | {test_name}")
    if details:
        print(f"         └─ {details}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST 1: EMBEDDING MODELS CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_embedding_models_config():
    print_header("TEST 1: Configuration des modèles d'embedding")
    
    from src.rag.embedder import EMBEDDING_MODELS, get_embedding_column
    
    # Check models exist
    expected_models = ["albert", "bge_scaleway", "qwen3_scaleway"]
    for model in expected_models:
        passed = model in EMBEDDING_MODELS
        print_result(f"Modèle '{model}' défini", passed)
        if passed:
            dims = EMBEDDING_MODELS[model]["dimensions"]
            print(f"         └─ Dimensions: {dims}")
    
    # Check column mapping
    test_cases = [
        ("rag_chunks_3", "albert", "embedding_m3"),
        ("rag_chunks_3", "bge_scaleway", "embedding_bge_scw"),
        ("rag_chunks_fiches_sp", "albert", "embedding"),
        ("rag_chunks_dgafp", "qwen3_scaleway", "embedding_qwen3"),
    ]
    
    for table, model, expected_col in test_cases:
        col = get_embedding_column(table, model)
        passed = col == expected_col
        print_result(f"Colonne {table}/{model}", passed, f"'{col}' (attendu: '{expected_col}')")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST 2: EMBEDDERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_embedders():
    print_header("TEST 2: Embedders (API calls)")
    
    from src.rag.embedder import AlbertEmbedder, ScalewayEmbedder, FallbackEmbedder
    
    test_query = "Quel est le délai de préavis pour un contractuel ?"
    
    # Test Albert
    try:
        albert = AlbertEmbedder(timeout=10)
        emb = albert.embed_query(test_query)
        passed = emb is not None and len(emb) == 1024
        print_result("AlbertEmbedder", passed, f"{len(emb) if emb else 0} dims")
    except Exception as e:
        print_result("AlbertEmbedder", False, str(e)[:50])
    
    # Test Scaleway BGE
    try:
        scaleway_bge = ScalewayEmbedder(model="bge-multilingual-gemma2", timeout=30)
        emb = scaleway_bge.embed_query(test_query)
        passed = emb is not None and len(emb) == 3584
        print_result("ScalewayEmbedder (BGE)", passed, f"{len(emb) if emb else 0} dims")
    except Exception as e:
        print_result("ScalewayEmbedder (BGE)", False, str(e)[:50])
    
    # Test Scaleway Qwen3
    try:
        scaleway_qwen = ScalewayEmbedder(model="qwen3-embedding-8b", timeout=30)
        emb = scaleway_qwen.embed_query(test_query)
        passed = emb is not None and len(emb) == 4096
        print_result("ScalewayEmbedder (Qwen3)", passed, f"{len(emb) if emb else 0} dims")
    except Exception as e:
        print_result("ScalewayEmbedder (Qwen3)", False, str(e)[:50])
    
    # Test FallbackEmbedder
    try:
        fallback = FallbackEmbedder(primary="albert", fallback="bge_scaleway", timeout=10)
        emb = fallback.embed_query(test_query)
        passed = emb is not None
        model_used = fallback.last_model_used
        print_result("FallbackEmbedder", passed, f"Modèle utilisé: {model_used}")
    except Exception as e:
        print_result("FallbackEmbedder", False, str(e)[:50])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST 3: RETRIEVER COLUMN SELECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_retriever_column_selection():
    print_header("TEST 3: Sélection dynamique de colonne (Retriever)")
    
    from src.rag.retriever import MultiSourcePGRetriever
    from src.rag.embedder import FallbackEmbedder
    
    # Create mock embedder
    class MockEmbedder:
        def __init__(self, model_key):
            self.last_model_used = model_key
        def embed_query(self, text):
            return [0.1] * 1024
    
    test_cases = [
        ("albert", "rag_chunks_3", "embedding_m3"),
        ("bge_scaleway", "rag_chunks_3", "embedding_bge_scw"),
        ("qwen3_scaleway", "rag_chunks_3", "embedding_qwen3"),
        ("albert", "rag_chunks_fiches_sp", "embedding"),
        ("bge_scaleway", "rag_chunks_dgafp", "embedding_bge_scw"),
    ]
    
    for model_key, table, expected_col in test_cases:
        mock_embedder = MockEmbedder(model_key)
        retriever = MultiSourcePGRetriever(
            dsn=None,  # No actual DB connection needed for this test
            embedder=mock_embedder,
            embedding_model_key=model_key,
        )
        
        col = retriever._get_embedding_column(table)
        passed = col == expected_col
        print_result(f"{model_key} → {table}", passed, f"'{col}'")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST 4: FULL RETRIEVAL (requires DB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_full_retrieval():
    print_header("TEST 4: Retrieval complet (avec DB)")
    
    dsn = os.getenv("PG_DSN") or os.getenv("SCALINGO_POSTGRESQL_URL")
    if not dsn:
        print("  ⚠️  Pas de DSN configuré - test skippé")
        print("      Lancez avec: PG_DSN=... python tests/test_embedding_fallback.py")
        return
    
    from src.rag.embedder import FallbackEmbedder
    from src.rag.retriever import MultiSourcePGRetriever
    
    test_query = "Quel est le délai de préavis pour un contractuel ?"
    
    # Test avec chaque modèle
    for model_key in ["albert", "bge_scaleway"]:
        try:
            # Create embedder (primary only, no fallback for this test)
            embedder = FallbackEmbedder(primary=model_key, fallback=None, timeout=30)
            
            # Create retriever
            retriever = MultiSourcePGRetriever(
                dsn=dsn,
                embedder=embedder,
                embedding_model_key=model_key,
                enable_deduplication=False,
                enable_mmr=False,
            )
            
            # Search
            chunks = retriever.search(test_query, k=3)
            passed = len(chunks) > 0
            
            # Get embedding column used
            col = retriever._get_embedding_column("rag_chunks_3")
            
            print_result(
                f"Retrieval avec {model_key}",
                passed,
                f"{len(chunks)} chunks, colonne: {col}"
            )
            
            if chunks:
                # Chunk uses 'text' attribute, not 'content'
                chunk_text = getattr(chunks[0], 'text', '') or getattr(chunks[0], 'content', '') or str(chunks[0])[:60]
                print(f"         └─ Premier chunk: {chunk_text[:60]}...")
                
        except Exception as e:
            print_result(f"Retrieval avec {model_key}", False, str(e)[:60])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST 5: FALLBACK SIMULATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fallback_simulation():
    print_header("TEST 5: Simulation de fallback")
    
    from src.rag.embedder import FallbackEmbedder, AlbertEmbedder
    
    # Create a mock that fails
    class FailingEmbedder:
        def embed_query(self, text):
            raise Exception("Simulated API failure")
        last_error = "Simulated failure"
    
    # Monkey-patch AlbertEmbedder to fail
    original_embed = AlbertEmbedder.embed_query
    
    def failing_embed(self, text):
        self._last_error = "Simulated Albert API failure"
        return None
    
    try:
        # Patch
        AlbertEmbedder.embed_query = failing_embed
        
        # Create FallbackEmbedder
        embedder = FallbackEmbedder(primary="albert", fallback="bge_scaleway", timeout=30)
        
        # This should trigger fallback to Scaleway
        emb = embedder.embed_query("Test query")
        
        passed = emb is not None and embedder.last_model_used == "bge_scaleway"
        print_result(
            "Fallback Albert → Scaleway",
            passed,
            f"Modèle utilisé: {embedder.last_model_used}, fallback_count: {embedder.fallback_count}"
        )
        
    finally:
        # Restore original
        AlbertEmbedder.embed_query = original_embed

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print("\n" + "🧪 " * 20)
    print("  TEST DU SYSTÈME DE FALLBACK EMBEDDINGS")
    print("🧪 " * 20)
    
    # Run tests
    test_embedding_models_config()
    test_embedders()
    test_retriever_column_selection()
    
    # Optional DB test
    if "--with-db" in sys.argv:
        test_full_retrieval()
    else:
        print_header("TEST 4: Retrieval complet (avec DB)")
        print("  ⏭️  Skippé. Lancez avec --with-db pour tester avec la DB")
    
    test_fallback_simulation()
    
    print("\n" + "="*60)
    print("  ✅ TESTS TERMINÉS")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()

