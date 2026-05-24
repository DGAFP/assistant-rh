# Scripts

Ce dossier contient des notebooks Jupyter pour extraire, nettoyer, chunker,
embedder et ingerer des sources RH dans Postgres/pgvector.

## Conventions et chemins
- La plupart des notebooks font un `cd ..` ou `cd assistant-rh` au debut pour
  pointer vers la racine du repo. Ajuster si besoin.
- Entrees et sorties principales: `data/in` et `data/out` a la racine du repo.
- `scripts/data/` sert de staging local pour certains runs; adapter les chemins
  si vous souhaitez l'utiliser.
- Embeddings: `BAAI/bge-m3` (dimension 1024).

## Prerequis
- Jupyter + libs Python: `pandas`, `numpy`, `pypdf`, `pdf2image`,
  `pytesseract`, `easyocr`, `sentence-transformers`, `fastparquet`, `psycopg`.
- OCR: `tesseract` + `poppler` (pour `pdf2image`). EasyOCR est optionnel.
- Ingestion: Postgres avec `pgvector` + variables d'environnement DB.

## Notebooks
- `scripts/analyse.ipynb`
  - Profiling/EDA (pandas, sklearn, ydata_profiling).
  - Genere `scripts/retex_profiling_report.html`.
- `scripts/extract_sp.ipynb`
  - Extraction Service-Public: lecture PDF/DOCX, nettoyage, OCR fallback,
    chunking Q/A.
  - Sorties: `data/out/chunked/chunks_qna_sp.jsonl` + embeddings
    `data/out/chunks_baai_bge_m3_with_emb_SP.jsonl`,
    `data/out/embeddings_baai_bge_m3_SP.npy`,
    `data/out/chunks_baai_bge_m3_SP.parquet`.
- `scripts/ingestion_SP.ipynb`
  - Ingere les JSONL avec embeddings SP vers Postgres (`rag_chunks_3`).
  - Config DB via `DATABASE_URL` ou variables `PG*`.
- `scripts/service_public_xml_example.ipynb`
  - Exemple de recuperation d'une fiche Service-Public via le flux XML officiel
    DILA (`data.gouv.fr` -> `vosdroits-latest.zip` -> `F12391.xml` -> markdown).
- `scripts/extract_matte.ipynb`
  - Extraction "matte" (temps_partiel / temps_de_travail, etc) avec OCR.
  - Sorties: `data/out/temps_partiel/*.txt`,
    `data/out/chunked/temps_partiel_chunks_qna.jsonl`,
    embeddings `data/out/temps_partiel_chunks_baai_bge_m3.*`.
- `scripts/amelioration_matte.ipynb`
  - Nettoyage + sectioning ameliore pour "temps du travail".
  - Sorties: `data/out/chunked/matte_temps_travail_3pdf_clean.jsonl`,
    embeddings `data/out/matte_temps_du_travail_amelioration_chunks_*.{parquet,npy,jsonl}`.
- `scripts/ingestion_matte.ipynb`
  - Ingere les JSONL issus d'`amelioration_matte` vers `rag_chunks_3`.
  - Utilise `data/out/temps_partiel_embeddings_baai_bge_m3.npy` pour
    determiner la dimension si besoin.
- `scripts/extract_circulaire.Ipynb`
  - Circulaires: OCR optionnel + chunking Q/A.
  - Sorties: `data/out/circulaires_txt`,
    `data/out/chunked/circulaires_chunks_qna.jsonl`,
    embeddings `data/out/circulaires_chunks_baai_bge_m3.*`.
- `scripts/extract_legifrance.ipynb`
  - Appels API Legifrance (OAuth; credentials actuellement en dur).
  - Sortie: `data/out/legifrance/legi_results.json`.
- `scripts/ingestion_legifrance.ipynb`
  - Embeddings a partir de `legi_results.json`, ingestion dans `law_chunks`.
- `scripts/extract_grrh.ipynb`
  - Agrege les `RGRH_*.xlsx` dans le repertoire courant.
  - Sortie: `all_RGRH_donnees_personnelles_rules_population_statut.csv`.
- `scripts/ingestion_rgrh.ipynb`
  - CSV -> Q/A -> embeddings -> Postgres (`rag_chunks_3`).
  - Lit `data/rgrh/entretien_professionnelle/all_RGRH_entretien_professionnelle_rules_population_statut.csv`.
  - Ecrit `data/out/chunked/chunks_from_csv_grouped_entretien_professionnelle.jsonl`.

## Flux suggere
1. `extract_*` (sources) -> (optionnel) `amelioration_matte`.
2. `ingestion_*` pour alimenter Postgres/pgvector.
