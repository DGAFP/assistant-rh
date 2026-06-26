# data/

Dossier de données pour l'assistant RH. Les fichiers volumineux ne sont pas
versionnés (gitignored).

## Contenu local

Les fichiers de données locales sont gitignored. Les artefacts d'évaluation restreints
(`golden_beta/`) sont stockés dans le dataset Hugging Face privé
`DGAFP/assistant-rh-private-data` et peuvent être récupérés avec :

```bash
HF_TOKEN=... moon run legacy:golden-beta-download
```

Voir `docs/data/PRIVATE_DATASETS.md`.

## Sources de données (en base)

Les données de production vivent dans PostgreSQL (Scalingo) :

| Table | Contenu |
|-------|---------|
| `rag_chunks_matte` | Guides pratiques MATTE |
| `rag_chunks_service_public` | Fiches Service Public |
| `rag_chunks_dgafp` | Textes réglementaires Legifrance + CGFP |
| `rag_chunks_rgrh` | Base RGRH |
| `rag_chunks_test` | Chunks Matte + Service-Public combinés |
| `rag_sections` | Sections markdown (contexte) |
| `rag_documents` | Métadonnées documents (titre, URL, publisher) |

Voir `docs/DATABASE.md` pour le schéma complet.
