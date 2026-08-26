# Décisions du chantier hexagonal-split

> Ce document regroupe les décisions actées et leur justification. La [vue d'ensemble](00-overview.md) reste volontairement courte ; le détail de mise en œuvre vit dans le [plan de migration](04-migration-plan.md).

## Architecture et contrat

| # | Décision | Pourquoi |
|---|---|---|
| D1 | **Contrat public OpenAI-compatible** (`/v1/chat/completions`, `/v1/models`), pas MCP | Le consommateur cible, `conversations`, parle ce contrat. Un MCP retrieval céderait la génération au client et contournerait la boucle qualité du RAG. Un adaptateur MCP reste possible plus tard. |
| D2 | **Routage ministère par le nom de modèle** : un modèle par ministère autorisé, `/v1/models` filtré par le bearer, fallback `default_ministry` | Le routage reste dans le contrat OpenAI et `conversations` sait déjà présenter un sélecteur de modèle. |
| D3 | **Core interne à l'API**, dans `apps/api/src/assistant_rh_api/core/` | Pas de cycle de release indépendant. Le runner goldset importe ce sous-module ; les règles d'import assurent son isolation. |
| D4 | **Le core garde la logique métier du retrieval et de la génération** : fusion, gates, sélection, composition des prompts et orchestration | C'est cette logique que les campagnes qualité mesurent. SQL et appels réseau restent derrière des ports étroits. |
| D5 | **À l'état cible, Streamlit ne touche plus Postgres** : chat et admin deviennent clients HTTP | Pendant le canary, le chemin direct reste disponible uniquement pour le rollback. `15_Import_Sources` reste hors de cette frontière, car il appartient au domaine ingestion Grist/S3. |
| D6 | **Même bearer pour chat et admin** : groupe résolu par token, rôle `is_admin` requis sur `/admin/*` | Une seule logique d'auth, sans token admin statique. Hash, rotation et bootstrap sont décrits dans le contrat API. |

## Migration et livraison

| # | Décision | Pourquoi |
|---|---|---|
| D7 | **PRs additives vers `dev`**, sans modifier le chemin de production pendant la reconstruction | L'API peut être revue, testée et déployée à côté de l'existant. Chaque PR reste petite et réversible. |
| D8 | **Parité suivie au [LEDGER](LEDGER.md)** : tout changement comportemental de l'ancien runtime est reporté dans la nouvelle implémentation ; gel court avant M3 | Le LEDGER rend visible la dérive entre les deux chemins qui cohabitent temporairement. |
| D9 | **Tests exacts sur fixtures/replays ; goldset live sur métriques et tolérances** | Les dépendances figées permettent une comparaison exacte. Avec un vrai LLM, on compare la qualité, pas l'identité du texte. |
| D10 | **Local et VM homelab avant le déploiement Scaleway staging** | Tests rapides en local, intégrations longues sur VM. Pas de troisième environnement temporaire. |
| D12 | **`apps/mastra-pipeline` supprimé dès la première PR d'implémentation** | Il est confirmé mort, sans consommateur ni déploiement, et sa suppression ne dépend pas de la bascule Python. |
| D14 | **Bascule réversible** : API dark → Streamlit sous feature flag → canary/stabilité → suppression de l'ancien chemin | Le déploiement de l'API et le retrait de `packages/rag-pipeline` ne se produisent jamais dans la même étape opérationnelle. |

## Exécution et données produit

| # | Décision | Pourquoi |
|---|---|---|
| D11 | **Feedback via `POST /v1/feedback`** ; l'id de completion correspond au `turn_id` du `chat_run` | La métrique produit survit à la séparation sans créer une seconde identité de run. |
| D13 | **État métier par requête**, sans singleton ni champ `last_*` partagé | Des requêtes concurrentes peuvent partager les pools/clients sûrs, jamais leurs résultats ou leurs traces. Voir `RunContext` dans l'architecture cible. |
