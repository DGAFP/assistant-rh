# Décisions du chantier hexagonal-split

> Ce document regroupe les décisions actées et leur justification. La [vue d'ensemble](00-overview.md) reste volontairement courte ; le détail de mise en œuvre vit dans le [plan de migration](04-migration-plan.md).

## Architecture et contrat

| # | Décision | Pourquoi |
|---|---|---|
| D1 | **Contrat public OpenAI-compatible** (`/v1/chat/completions`, `/v1/models`), pas MCP | Le consommateur cible, `conversations`, parle ce contrat. Un MCP retrieval céderait la génération au client et contournerait la boucle qualité du RAG. Un adaptateur MCP reste possible plus tard. |
| D2 | **Routage ministère par le nom de modèle** : un modèle par ministère autorisé, `/v1/models` filtré par le bearer, fallback `default_ministry` | Le routage reste dans le contrat OpenAI et `conversations` sait déjà présenter un sélecteur de modèle. |
| D3 | **Core hexagonal interne à l'application API**, dans `apps/api/src/assistant_rh_api/core/` | Aucun consommateur n'a besoin de publier ou versionner le core indépendamment de l'API. Le runner goldset peut importer ce sous-module directement. Un package `packages/rag-core` ajouterait une frontière de packaging sans autonomie réelle. La frontière utile est imposée par les imports : `core` n'importe ni handlers, ni DB, ni gateways. Si un second produit ou un cycle de release indépendant apparaît, l'extraction en package restera mécanique. |
| D4 | **Le core garde la logique métier du retrieval et de la génération** : fusion, gates, sélection, composition des prompts et orchestration | C'est cette logique que les campagnes qualité mesurent. SQL et appels réseau restent derrière des ports étroits. |
| D5 | **À l'état cible, Streamlit ne touche plus Postgres** : chat et admin deviennent clients HTTP | Pendant le canary, le chemin direct reste disponible uniquement pour le rollback. `15_Import_Sources` reste hors de cette frontière, car il appartient au domaine ingestion Grist/S3. |
| D6 | **Même mécanisme bearer pour les routes publiques et admin** : le token résout un groupe ; `/admin/*` exige le rôle `is_admin` | Cela évite un `ADMIN_TOKEN` statique et une seconde logique d'auth. Les tokens sont hashés, rotatifs et jamais renvoyés après leur création. Un mécanisme de bootstrap permet de créer ou réinitialiser le premier groupe admin sans endpoint admin déjà authentifié. |

## Migration et livraison

| # | Décision | Pourquoi |
|---|---|---|
| D7 | **PRs additives vers `dev`**, sans modifier le chemin de production pendant la reconstruction | L'API peut être revue, testée et déployée à côté de l'existant. Chaque PR reste petite et réversible. |
| D8 | **Parité suivie au [LEDGER](LEDGER.md)** : tout changement comportemental de l'ancien runtime est reporté dans la nouvelle implémentation ; gel court avant M3 | Le LEDGER rend visible la dérive entre les deux chemins qui cohabitent temporairement. |
| D9 | **Deux preuves différentes** : tests déterministes exacts sur fixtures/replays, puis éval goldset live sur métriques et tolérances | Avec des entrées et dépendances figées, les sorties d'étapes et l'enveloppe API doivent être identiques. Avec de vrais appels LLM, le texte varie : on compare alors les métriques de qualité, pas les chaînes octet par octet. |
| D10 | **Deux environnements de travail avant Scaleway** : local et VM homelab. Scaleway staging est le premier déploiement cloud de l'API complète | Le local couvre les tests rapides et synthétiques ; la VM couvre les intégrations longues, providers réels et données staging. Il n'y a pas de troisième environnement ou de container Scaleway éphémère pendant la construction. |
| D12 | **`apps/mastra-pipeline` supprimé dès la première PR d'implémentation** | Il est confirmé mort, sans consommateur ni déploiement, et sa suppression ne dépend pas de la bascule Python. |
| D14 | **Bascule réversible** : API dark → Streamlit sous feature flag → canary/stabilité → suppression de l'ancien chemin | Le déploiement de l'API et le retrait de `packages/rag-pipeline` ne se produisent jamais dans la même étape opérationnelle. |

## Exécution et données produit

| # | Décision | Pourquoi |
|---|---|---|
| D11 | **Feedback via `POST /v1/feedback`** ; l'id de completion correspond au `turn_id` du `chat_run` | La métrique produit survit à la séparation sans créer une seconde identité de run. |
| D13 | **État métier local à chaque requête** : ministère, `turn_id`, traces, diagnostics et résultat ne vivent jamais dans un singleton ou un champ `last_*` | Deux requêtes FastAPI peuvent s'exécuter en parallèle. Elles peuvent partager un pool DB ou un client HTTP thread-safe, mais jamais une donnée propre à un utilisateur ou à un run. Les étapes retournent explicitement leurs résultats dans le `RunContext`. |

