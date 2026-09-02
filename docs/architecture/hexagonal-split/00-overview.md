# Chantier hexagonal-split — Vue d'ensemble

> Statut : plan amendé après revue, mise à jour du 2026-08-26.
> Portée : transformation du monolithe en architecture front & back hexagonale, contrat public OpenAI-compatible.
> Documents du dossier : [décisions](06-decisions.md) · [architecture cible](01-target-architecture.md) · [contrat API](02-api-contract.md) · [diagrammes de séquence](03-sequence-diagrams.md) · [plan de migration](04-migration-plan.md) · [spike clients A2](07-openai-client-spike.md) · [LEDGER](LEDGER.md)

## Problème

Le runtime actuel est couplé à Streamlit : `packages/rag-pipeline` mêle métier, SQL et appels providers, et `src/ui` porte une partie de l'orchestration. Le chat utilise des imports Python directs. Le futur front, La Suite `conversations`, a besoin d'un contrat HTTP OpenAI-compatible.

## Solution en deux temps

**Temps 1 (ce chantier)** — construire `apps/api` à côté du runtime existant : DB/adaptateurs d'abord, puis extraction progressive de la logique vers `assistant_rh_api/core`. Déployer l'API, adapter le chat et l'admin Streamlit sous feature flags, puis retirer l'ancien chemin après stabilité.

**Temps 2 (chantier ultérieur)** — un fork de [suitenumerique/conversations](https://github.com/suitenumerique/conversations) (adapté à nos besoins, dont le feedback) remplace le chat Streamlit ;‡ Streamlit devient une pure interface d'admin ; ProConnect vit dans le front, jamais dans l'API RAG.

```mermaid
flowchart LR
    subgraph "Temps 1"
        ST1[Streamlit chat + admin] -->|feature flag HTTP| API1[apps/api dark puis canary]
        ST1 -.->|rollback temporaire| OLD[packages/rag-pipeline]
        API1 --> DB1[(Postgres)]
        OLD --> DB1
    end
    subgraph "Temps 2"
        CONV[fork conversations + ProConnect] -->|OpenAI-compat| API2[apps/api]
        ST2[Streamlit admin] -->|/admin/*| API2
        API2 --> DB2[(Postgres)]
    end
```

## Décisions actées

Voir [06-decisions.md](06-decisions.md), organisé en architecture/contrat, migration/livraison et exécution/données produit.

## Inventaire de fin de chantier

**Supprimé en A1 ([#440](https://github.com/DGAFP/assistant-rh/issues/440))** : l'ancien pipeline TypeScript, ses workflows et ses scripts de conformance dédiés.

**À supprimer seulement après stabilité de la bascule** : `packages/rag-pipeline` · les modules `src/ui/chatbot_*` du chemin direct-import · l'accès DB direct de Streamlit · les flags de rollback.

**Créé** : `apps/api/` (`core`, handlers, wiring, db, gateways) · `docker/api/Dockerfile` · runners de conformance déterministe et d'éval via-API · garde CI de frontière d'imports · migration d'auth API · fixtures runtime locales sans données personnelles.

**Conservé / adapté** : `apps/streamlit-ui` (ancien chemin conservé pendant le canary, puis chat/admin clients HTTP ; `15_Import_Sources` inchangé) · `src/goldset` + skill `run-rag-eval` repointés sur `assistant_rh_api.core` avec adaptateurs explicites · `packages/data-engineering` + `apps/data-ingestion-cli` intouchés · `packages/shared-config` conservé.

## Risques principaux

1. **Inventaire d'I/O incomplet** (SQL, prompts, acronymes, caches, provider, état mutable au-delà de `retriever.py`) → audit A5 systématique avant les découpes ; chaque dépendance devient un port ou une donnée de requête.
2. **Régression qualité silencieuse pendant la reconstruction parallèle** → conformance déterministe à chaque PR, pytest vert, évals live aux jalons (voir [plan de migration](04-migration-plan.md)).
3. **Dérive entre ancien et nouveau core** → reports notés au LEDGER, gel final court, aucune amélioration opportuniste dans le portage.
4. **Timeouts / comportement SSE sur Scaleway serverless ou incompatibilité `conversations`** → contrat testé en local/homelab, puis validation de la plateforme dès le premier déploiement dark en staging.
5. **Bascule API + Streamlit couplée** → API dark et observable d'abord ; feature flag et ancien chemin conservé pendant la fenêtre de stabilité.
6. **Perte de fonctions admin/feedback** → matrice de parité page par page ; aucune page fonctionnelle n'est archivée sans décision produit explicite.

## Points ouverts

- Validation DINUM/DGAFP du déploiement du fork `conversations` et de l'auth fork → API. Le spike technique A2 précède le handler completion ; la décision organisationnelle peut suivre.
- Sort des pages DB/éval/debug : réintégration via API, maintien temporaire ou archivage approuvé (matrice A3, avant les endpoints concernés).
