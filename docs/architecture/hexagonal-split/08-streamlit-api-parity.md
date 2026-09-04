# Arbitrage A3 — parité Streamlit et périmètre API

> Statut : **acté le 2026-09-04** pour le chemin public du chat. Les migrations de l'admin, de l'observabilité et des outils qualité sont reportées et ne bloquent pas M4.
> Références : [contrat API](02-api-contract.md) · [plan de migration](04-migration-plan.md) · [décisions](06-decisions.md) · [audit A5](07-runtime-isolation-audit.md).

Ce document ferme la matrice demandée par l'issue [#444](https://github.com/DGAFP/assistant-rh/issues/444). Il sépare le chemin public, qui devient client HTTP sans accès PostgreSQL ni import du pipeline Python, de l'interface Streamlit d'administration et d'exploitation, qui conserve temporairement un accès DB direct sous une exception explicite.

## Décisions structurantes

1. **M4 porte sur le chemin public.** `Home.py`, `01_Chatbot` et `_PDF_Viewer` utilisent l'API. Ils n'importent plus de client PostgreSQL ni `packages/rag-pipeline` après le retrait du rollback.
2. **Streamlit reste l'outil admin/ops.** Chat Logs, Feedback Dashboard, Admin Config, DB Explorer, Goldset Explorer, les pages d'évaluation, Pipeline Timeline et User Groups restent dans `apps/streamlit-ui`, protégés par `require_admin()`, avec leurs accès DB actuels.
3. **Cette exception ne bloque pas M4.** Une migration vers Grafana/Tempo, LangSmith, un outil RAG-ops ou des endpoints admin sera instruite séparément, avec une décision d'hébergement et de traitement des données sensibles.
4. **Aucune API SQL générique n'est créée.** Le maintien de pages admin en accès direct n'autorise aucun endpoint de requête arbitraire dans `apps/api`.
5. **Le login crée une session API courte.** Après vérification du mot de passe, l'API émet un bearer opaque, borné au groupe, valable huit heures et conservé uniquement côté serveur Streamlit. Il n'existe pas de bundle `STREAMLIT_API_BEARERS_JSON`.
6. **Les liens internes ne contiennent pas de capability durable.** `chat_run_sources` persiste seulement les sources finales affichées. Au clic, le frontend authentifié demande une URL valable quinze minutes ; sa présignature S3 ne dépasse pas la durée restante et un échec déclenche au plus une nouvelle demande authentifiée.
7. **Le feedback suit l'UI active.** L'API accepte une note 1–5, les raisons positives/négatives cochées et un commentaire. Elle dérive `helpful`, normalise le stockage historique 0–4, migre les doublons existants sans perte et conserve un audit des remplacements corrélé au groupe et à une session pseudonyme.
8. **L'agentic RAG reste hors de ce chantier iso-fonctionnel.** Il est traité comme une expérimentation qualité ultérieure ; LangSmith éventuel n'impose aucune adoption de LangChain.

## Catégories de décision

- **Client HTTP public** : le parcours public appelle uniquement les routes `/v1/*` listées ci-dessous.
- **Exception admin directe** : la page Streamlit reste protégée par l'auth admin et accède directement à PostgreSQL avec des permissions bornées.
- **Outil ingestion** : la fonction reste dans le domaine Grist/S3 existant.
- **Archivage antérieur** : la fonction avait déjà un remplacement documenté avant A3.

## Rôles et frontières d'autorisation

| Rôle | Authentification | Accès permis | Interdictions et données sensibles |
|---|---|---|---|
| Probe | aucune | `GET /healthz` | Aucun détail de secret, DSN, provider ou corpus. |
| Visiteur Streamlit | aucune | catalogue des groupes visibles et création d'une session par mot de passe | Réponses génériques et rate limiting obligatoire ; mot de passe jamais logué. |
| Groupe | bearer opaque de session, durée 8 h | modèles autorisés, chat, feedback de ses runs, demande d'URL documentaire | Ministères filtrés par la politique du groupe ; un run hors groupe répond 404. |
| Administrateur Streamlit | auth admin existante | pages admin/ops et accès DB explicitement allowlisté | Questions, réponses, sessions, traces, prompts et gold answers sont sensibles ; pas de rôle propriétaire/migration pour les lectures courantes. |
| Ingestion | admin Streamlit + secrets Grist/S3 côté serveur | dépôt de documents et mise à jour du manifeste | Aucun secret Grist/S3 dans le navigateur, les logs ou les exports. |

## Matrice page par page

| Page / fonction actuelle | Décision cible | Remplacement ou maintien | Transition et propriétaire |
|---|---|---|---|
| `Home.py` — groupes visibles, login par mot de passe, cookie de groupe | **Client HTTP public** | `GET /v1/auth/groups`, `POST /v1/auth/session`, `GET /v1/auth/me` | B4 livre l'auth ; E1 migre le client. DB direct maintenu seulement dans le rollback public jusqu'à F3. |
| `01_Chatbot` — modèles/ministères, historique cinq tours, stream, sources | **Client HTTP public** | `GET /v1/models`, `POST /v1/chat/completions`, demande d'URL documentaire | C1–C7 puis E1. `RAG_CHAT_BACKEND=direct|api` jusqu'à F3. |
| `01_Chatbot` — feedback structuré | **Client HTTP public** | `POST /v1/feedback` | D1 puis E1 ; ownership groupe/run obligatoire. |
| `02_Chat_Logs` — filtres, tableau, détail, métriques pipeline | **Exception admin directe** | page Streamlit et tables `chat_runs`/`chat_feedbacks` existantes | Reste en place après M4. Migration d'observabilité séparée. |
| `03_Feedback_Dashboard` — métriques, raisons, analyse et exports | **Exception admin directe** | page Streamlit et jobs existants | Reste en place après M4 ; aucun endpoint admin requis par ce chantier. |
| `04_Admin_Config` — config, prompts et acronymes | **Exception admin directe** | page Streamlit existante | Mutations DB conservées sous auth admin. API admin reportée. |
| `05_DB_Explorer` — stats, documents, chunks et sections | **Exception admin directe** | vue Streamlit existante | Lecture du corpus réservée aux admins. Aucun endpoint SQL/corpus. |
| `06_Goldset_Explorer` — curation, imports et exports | **Exception admin directe** | page Streamlit existante | Mutations goldset conservées ; migration RAG-ops éventuelle hors chantier. |
| `08_Chunking_Evaluation` | **Exception admin directe** | runner et vue Streamlit existants | Accès corpus/goldset et écritures d'expériences conservés. |
| `09_Pipeline_Evaluation` | **Exception admin directe** | runner et vue Streamlit existants | Son repointage, avec celui des autres consommateurs admin, appartient à `admin-hardening` avant retrait de `packages/rag-pipeline`. |
| `10_Intent_Gater_Evaluation` | **Exception admin directe** | runner et vue Streamlit existants | Le retrait de son DDL runtime devient un suivi d'admin-hardening non bloquant pour M4. |
| `12_Pipeline_Timeline` | **Exception admin directe** | page Streamlit et `rag_trace_events` | Reste en place après M4. Grafana/Tempo ou LangSmith seront évalués séparément. |
| `13_admin` | **Exception admin directe** | raccourci Streamlit existant | Aucun endpoint propre. |
| `14_User_Groups` | **Exception admin directe** | CRUD et reset de mot de passe existants | La gestion de bearers longue durée est retirée du périmètre. |
| `15_Import_Sources` | **Outil ingestion** | domaine ingestion Grist/S3 existant | Inchangé ; propriétaire data engineering. |
| `_PDF_Viewer` | **Client HTTP public** | référence stable puis URL signée à la demande ; rédemption API des bytes legacy ou redirection S3 | Plus aucun accès DB/S3 direct depuis le viewer public après F3. |
| `archive/07_Eval_Comparison` | **Archivage antérieur** | `rag_quality_eval_runs` + journal/runner | Décision antérieure conservée. |
| `archive/11_Golden_Beta_Analysis` | **Archivage antérieur** | `03_Feedback_Dashboard` | Décision antérieure conservée. |

Après F3, la documentation classe comme **legacy** les pages d'évaluation qui exécutent encore `packages/rag-pipeline` : elles restent consultables, sans avertissement UI obligatoire puisqu'elles ne sont pas publiques, mais ne constituent plus une preuve sur le runtime public. Le runner D3 via API et son journal deviennent la preuve canonique M2/M3/M4 jusqu'à leur repointage dans `admin-hardening`.

## Exception Streamlit admin → PostgreSQL

L'accès direct des pages admin est une architecture intermédiaire acceptée, sans date de retrait arbitraire. Il ne bloque ni la bascule du chat, ni F3, ni M4.

Garde-fous bloquants pour M4 :

- toutes les pages concernées appellent `require_admin()` avant leur logique applicative ;
- la CI maintient une allowlist explicite des modules Streamlit autorisés à importer un client DB ;
- un smoke vérifie que les pages admin conservées restent chargeables après F3 ;
- `Home.py`, `01_Chatbot`, `_PDF_Viewer` et leurs helpers publics sont interdits d'accès direct DB après F3.

Le chantier `admin-hardening`, explicitement non bloquant pour M4, porte la restriction réseau, les identifiants DB dédiés et bornés, l'audit des actions sensibles, le déplacement du DDL runtime historique vers des migrations, le repointage de tous les consommateurs admin puis la suppression de `packages/rag-pipeline`. Les nouveaux schémas livrés par l'API continuent dès maintenant à passer par des migrations versionnées.

L'exception est réexaminée lorsqu'un remplaçant admin/ops est financé et satisfait les besoins fonctionnels, ou si l'accès externe, un incident de sécurité ou le couplage au schéma impose une séparation. Les options Grafana/Tempo, LangSmith et RAG-ops restent des pistes, pas des dépendances du chantier actuel.

## Registre des endpoints publics

| Endpoint | Livraison | Autorisation | Données et règle de sécurité |
|---|---|---|---|
| `GET /healthz` | A4, existant | aucune | État synthétique uniquement. |
| `GET /v1/auth/groups` | B4 | aucune | Métadonnées d'affichage des groupes visibles, non-admin et loginables. |
| `POST /v1/auth/session` | B4 | aucune, rate-limitée | Reçoit slug/mot de passe ; retourne un bearer opaque de 8 h et l'identité non secrète. 401 identique pour groupe absent ou mot de passe faux. |
| `GET /v1/auth/me` | B4 | bearer de session | Identité, politique ministère, expiration et `credential_revision`. |
| `GET /v1/models` | B5 | bearer de session | Catalogue filtré par `allowed_ministries`. |
| `POST /v1/chat/completions` | C1–C7 | bearer de session | Question, historique, réponse et références de sources ; aucun secret durable dans le contenu. |
| `POST /v1/feedback` | D1 | bearer propriétaire du run | Feedback structuré ; 404 pour run absent ou hors groupe ; upsert audité. |
| `POST /v1/documents/{doc_ref}/access-url` | D2 | bearer propriétaire/autorisé | Vérifie que le document appartient aux sources du run et retourne une URL valable 15 min. |
| `GET /v1/documents/access/{capability}` | D2 | capability courte dans l'URL | Streame les bytes legacy ou redirige vers S3 ; portée à un document, expiration 15 min, masquée dans les logs. |

Les routes authentifiées sont utilisées par le backend Streamlit ; seule la route de rédemption est suivie directement par le navigateur avec sa capability courte. L'absence de CORS n'est pas considérée comme un contrôle d'accès : la vérification de mot de passe est protégée par quotas IP + slug, backoff et métriques sans données de mot de passe.

## Session API courte

- `POST /v1/auth/session` émet un token opaque borné au groupe et à ses `allowed_ministries` pendant huit heures.
- Le token est conservé dans l'état serveur du frontend, jamais dans le cookie Streamlit, une URL, un log ou un artefact CI.
- Il n'est pas renouvelé silencieusement : à expiration, une nouvelle authentification est requise.
- Un reset de mot de passe incrémente `credential_revision` et invalide les sessions antérieures.
- Le resolver revérifie le groupe, sa visibilité, son rôle et sa révision ; il échoue fermé si le groupe a été désactivé.
- Le mécanisme machine-to-machine du futur front `conversations` sera décidé pendant la conception du fork au temps 2 et ne réintroduit pas un bundle par groupe dans Streamlit.

## Feedback public

La requête canonique correspond au widget actif :

```json
{
  "completion_id": "chatcmpl-<turn_id>",
  "stars": 4,
  "reasons_positive": ["Clair", "Utile"],
  "reasons_negative": ["Incomplet"],
  "comment": "Il manque la procédure détaillée."
}
```

- `stars` est obligatoire, entier 1–5 ; `rating` n'appartient pas au contrat canonique.
- Au moins une raison cochée ou un commentaire non vide est requis, comme dans l'UI actuelle.
- `helpful` est dérivé côté serveur : 1–2 → faux, 3–5 → vrai.
- Pendant la coexistence, l'adaptateur persiste `stars - 1` afin de conserver l'encodage historique 0–4 ; les lectures admin existantes restent inchangées.
- Le catalogue correspond au widget actif : positif `Clair`, `Utile`, `Pertinent`, `Complet`, `Précis` ; négatif `Confus`, `Éléments faux`, `Non pertinent`, `Incomplet`, `Sources manquantes`.
- Les combinaisons suivent le rendu de l'UI : raisons négatives seules pour 1–2, deux listes pour 3–4, raisons positives seules pour 5 ; les écarts sont rejetés en 422.
- Avant l'unicité, une migration versionnée conserve comme valeur courante le dernier feedback par `(ts DESC NULLS LAST, id DESC)` et copie chaque doublon antérieur, avec son id et son horodatage, dans `chat_feedback_audit`. Elle ajoute ensuite la contrainte unique sur `chat_feedbacks(turn_id)` ; ce nettoyage précède le déploiement de D1.
- Le serveur normalise les raisons dans l'ordre du catalogue et nettoie le commentaire, puis calcule une empreinte canonique. La transaction verrouille le `chat_run` parent pour sérialiser aussi deux toutes premières soumissions. Un retry identique est un no-op sans nouvel audit ; une charge différente archive la valeur précédente puis remplace atomiquement la valeur courante.
- Une modification conserve les annotations humaines `beta_scope` et `theme`, mais efface `error_category`, `ai_reason` et `ai_analyzed_at` pour permettre une nouvelle analyse IA.
- L'audit append-only porte le groupe et un `audit_session_hash` stable pendant la session de huit heures : HMAC-SHA-256 avec clé dédiée d'un identifiant interne aléatoire, jamais du bearer, du cookie ou d'une identité utilisateur. Il distingue des sessions, pas des personnes, et change après expiration, logout ou nouvelle authentification ; le rôle runtime n'a aucun droit d'update/delete sur l'audit.
- L'ownership est vérifié sur le groupe de la session ; run absent et run hors groupe répondent tous deux 404.

## Sources finales et traces

- `chat_run_sources(turn_id, doc_ref, ordinal, …)` contient uniquement les sources effectivement sélectionnées et retournées à l'utilisateur, dans leur ordre d'affichage. C'est la liste d'autorisation de la route documentaire.
- Les candidats récupérés, scores/rangs, filtres, rejets, fallbacks et décisions des différentes étapes sont consignés dans `rag_trace_events`. Ils restent diagnostiques et n'accordent aucun droit documentaire.
- `chat_runs`, `chat_run_sources` et `rag_trace_events` sont commités atomiquement avant le chunk terminal et `[DONE]` ; une transaction échouée ne publie pas une completion prétendument réussie.

## Accès documentaire court

- Une source publique conserve son URL canonique.
- Une source interne persiste seulement `doc_ref`, `turn_id` et son ordinal dans `chat_run_sources`, jamais une capability ou une URL signée.
- Le frontend authentifié appelle `POST /v1/documents/{doc_ref}/access-url` au moment du clic, avec le `completion_id` concerné.
- Le serveur vérifie que le document figure dans les sources persistées de ce run et que la session appartient au groupe autorisé.
- La réponse contient une URL opaque valable quinze minutes et `expires_at`. Elle n'est pas ajoutée à l'historique de conversation. Chaque clic repart de `doc_ref` + `completion_id` et obtient une URL fraîche.
- `GET /v1/documents/access/{capability}` streame les bytes d'un document legacy conservé en PostgreSQL ou redirige vers une URL S3 présignée. La capability remplace le bearer pour cette navigation, ne couvre qu'un document, expire après quinze minutes, n'est pas persistée et est masquée dans les logs d'accès ; le contenu et les paramètres de réponse S3 imposent `Cache-Control: private, no-store` et un `Content-Disposition` assaini.
- La durée de la présignature S3 est au plus la durée restante de la capability. Sur un premier échec de rédemption, le frontend encore authentifié redemande automatiquement une capability puis réessaie une fois ; le `POST` refait les contrôles run/groupe/source et aucun lien expiré ne peut se renouveler seul.
- Les routes n'autorisent ni listing ni autre document. Les échecs d'existence et d'autorisation répondent tous deux 404.
- A2 a validé uniquement le transport OpenAI du provider `conversations`. Le rendu et le renouvellement de ces sources, comme son auth machine-to-machine et son feedback, appartiennent au fork du temps 2 ; un document interne peut rester non cliquable dans un client OpenAI générique qui n'implémente pas l'extension.

## Surface admin reportée

Aucun endpoint `/admin/*` n'est requis pour M4. Les contrats précédemment envisagés pour config, prompts, acronymes, groupes, tokens, chat-runs, traces, feedbacks et jobs d'analyse sont retirés de la v1 et pourront faire l'objet d'un ADR et de PRs dédiées.

Ce report ne change pas les autorisations actuelles : les fonctions restent derrière `require_admin()` dans Streamlit. Il n'autorise pas l'exposition des tables dans l'API publique.

## Conditions d'acceptation A3

- le chemin public fonctionne via HTTP sous feature flag, avec rollback testé jusqu'à F3 ;
- les sessions expirent après huit heures et sont invalidées par reset de mot de passe ;
- les feedbacks suivent le schéma étoiles/raisons/commentaire et vérifient l'ownership ;
- la migration feedback conserve le dernier `(ts, id)`, archive tous les doublons et résiste à deux premières soumissions concurrentes ;
- les sources finales autorisées et les traces de toutes les étapes sont persistées atomiquement avec le run ;
- aucune URL signée ou capability documentaire durable n'est persistée ;
- la présignature S3 n'excède pas la durée restante de sa capability et le client ne retente qu'une fois, par une nouvelle demande authentifiée ;
- les pages admin existantes restent fonctionnelles et protégées ;
- M4 vérifie l'absence d'accès DB et d'import pipeline dans le chemin public, pas dans toute l'application Streamlit.
