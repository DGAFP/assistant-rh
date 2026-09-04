# Décisions du chantier hexagonal-split

> Ce document regroupe les décisions actées et leur justification. La [vue d'ensemble](00-overview.md) reste volontairement courte ; le détail de mise en œuvre vit dans le [plan de migration](04-migration-plan.md).

## Architecture et contrat

| # | Décision | Pourquoi |
|---|---|---|
| D1 | **Contrat public OpenAI-compatible** (`/v1/chat/completions`, `/v1/models`), pas MCP | Le consommateur cible, `conversations`, parle ce contrat. Un MCP retrieval céderait la génération au client et contournerait la boucle qualité du RAG. Un adaptateur MCP reste possible plus tard. |
| D2 | **Routage ministère par le nom de modèle** : un modèle par ministère autorisé, `/v1/models` filtré par le bearer, fallback `default_ministry` | Le routage reste dans le contrat OpenAI et `conversations` sait déjà présenter un sélecteur de modèle. |
| D3 | **Core interne à l'API**, dans `apps/api/src/assistant_rh_api/core/` | Pas de cycle de release indépendant. Le runner goldset importe ce sous-module ; les règles d'import assurent son isolation. |
| D4 | **Le core garde la logique métier du retrieval et de la génération** : fusion, gates, sélection, composition des prompts et orchestration | C'est cette logique que les campagnes qualité mesurent. SQL et appels réseau restent derrière des ports étroits. |
| D5 | **À M4, le chemin public Streamlit ne touche plus PostgreSQL et n'importe plus le pipeline Python** | L'admin/ops Streamlit conserve un accès DB direct sous exception allowlistée et `require_admin()`. Cette exception ne bloque pas la bascule du chat ni le retrait de `packages/rag-pipeline`. |
| D6 | **Le login émet une session API opaque, bornée au groupe et valable huit heures** | Streamlit conserve le bearer uniquement côté serveur. Il n'existe ni token admin statique, ni bundle de bearers par groupe dans une variable d'environnement. Un reset de mot de passe invalide les sessions antérieures. |

## Migration et livraison

| # | Décision | Pourquoi |
|---|---|---|
| D7 | **PRs additives vers `dev`**, sans modifier le chemin de production pendant la reconstruction | L'API peut être revue, testée et déployée à côté de l'existant. Chaque PR reste petite et réversible. |
| D8 | **Parité suivie au [LEDGER](LEDGER.md)** : tout changement comportemental de l'ancien runtime est reporté dans la nouvelle implémentation ; gel court avant M3 | Le LEDGER rend visible la dérive entre les deux chemins qui cohabitent temporairement. |
| D9 | **Tests exacts sur fixtures/replays ; goldset live sur métriques et tolérances** | Les dépendances figées permettent une comparaison exacte. Avec un vrai LLM, on compare la qualité, pas l'identité du texte. |
| D10 | **Local et VM homelab avant le déploiement Scaleway staging** | Tests rapides en local, intégrations longues sur VM. Pas de troisième environnement temporaire. |
| D12 | **Ancien pipeline TypeScript supprimé par A1 ([#440](https://github.com/DGAFP/assistant-rh/issues/440))** | Il était confirmé mort, sans consommateur ni déploiement, et sa suppression ne dépendait pas de la bascule Python. |
| D14 | **Bascule réversible** : API dark → Streamlit sous feature flag → canary/stabilité → suppression de l'ancien chemin | Le déploiement de l'API et le retrait de `packages/rag-pipeline` ne se produisent jamais dans la même étape opérationnelle. |

## Exécution et données produit

| # | Décision | Pourquoi |
|---|---|---|
| D11 | **Feedback via `POST /v1/feedback`** ; l'id de completion correspond au `turn_id` du `chat_run` | La métrique produit survit à la séparation sans créer une seconde identité de run. |
| D13 | **État métier par requête**, sans singleton ni champ `last_*` partagé | Des requêtes concurrentes peuvent partager les pools/clients sûrs, jamais leurs résultats ou leurs traces. Voir `RunContext` dans l'architecture cible. |

## Parité Streamlit A3

| # | Décision | Pourquoi |
|---|---|---|
| D15 | **Aucune nouvelle fonction active n'est archivée et aucune reconstruction RAG-ops ne bloque M4** | Chat Logs, Feedback Dashboard, Admin Config, DB/Goldset Explorer, évaluations, Pipeline Timeline et User Groups restent dans Streamlit sous auth admin. Grafana/Tempo, LangSmith, RAG-ops et les endpoints admin sont des chantiers ultérieurs. |
| D16 | **L'accès DB direct de l'admin Streamlit est une exception acceptée et gardée** | La CI allowliste les modules, `require_admin()` est testé, les identifiants DB sont bornés et le nouveau DDL passe par migrations. Le DDL runtime historique restant est une dette de durcissement non bloquante. |
| D17 | **Les documents internes utilisent une URL signée de quinze minutes, créée au clic** | Seuls `doc_ref` et `turn_id` sont persistés. Le frontend authentifié demande une URL fraîche ; aucune capability durable ne fuit dans l'historique, les traces ou les exports. |
| D18 | **Le feedback canonique est étoiles 1–5 + raisons cochées + commentaire** | `helpful` est dérivé, le stockage reste 0–4 pendant la coexistence, un second envoi remplace la valeur courante de façon idempotente et l'ancienne valeur reste auditée. |

## Règles de frontière gardées par la CI

1. `assistant_rh_api.core` n'importe ni `handlers`, ni `db`, ni `gateways`, ni `psycopg`, `fastapi`, `httpx`, `streamlit` ou `boto3`.
2. `handlers/` n'importe pas `psycopg` et ne contient pas de logique métier ; il valide le transport puis appelle un cas d'usage.
3. `db/` et `gateways/` n'importent pas `handlers` ; ils implémentent les `Protocol` de `assistant_rh_api.core.ports`.
4. `assistant_rh_api/__init__.py` reste sans effet de bord afin que `src/goldset` importe le core sans créer FastAPI ni ouvrir de connexion.
5. Le wiring vit dans `handlers/app.py` pour l'API et dans le runner direct-core pour l'éval.
6. Après F3, le chemin public (`Home.py`, `01_Chatbot`, `_PDF_Viewer` et leurs helpers) n'importe ni client PostgreSQL ni `packages/rag-pipeline` ; il utilise HTTP. Les modules admin autorisés à accéder à la DB figurent dans une allowlist CI distincte et restent protégés par `require_admin()`.
