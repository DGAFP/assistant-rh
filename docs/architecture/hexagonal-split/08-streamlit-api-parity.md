# Arbitrage A3 — parité Streamlit et surface API d'administration

> Statut : **acté le 2026-09-02** pour les phases D1, D2, D3, E1 et E2.
> Références : [contrat API](02-api-contract.md) · [plan de migration](04-migration-plan.md) · [décisions](06-decisions.md) · [audit A5](07-runtime-isolation-audit.md).

Ce document ferme la matrice demandée par l'issue [#444](https://github.com/DGAFP/assistant-rh/issues/444). Il distingue le produit Streamlit public, qui devient un client HTTP sans accès Postgres, des outils d'exploitation et de qualité qui ont besoin d'un accès direct et borné aux données.

## Décisions structurantes

1. **Le périmètre produit reste iso-fonctionnel.** Chat, authentification de groupe, logs, feedback, configuration, prompts, acronymes, traces et groupes sont servis par les routes figées ci-dessous. Les capacités corpus/goldset/éval restent disponibles dans RAG-ops. L'accès DB direct du produit ne subsiste que derrière le flag de rollback jusqu'à F3.
2. **Le corpus, le goldset et les expérimentations ne deviennent pas une API SQL.** Les pages `05`, `06`, `08`, `09` et `10` sont déplacées, avec leurs fonctions actuelles, dans un outil RAG-ops interne, hors du déploiement public et avec des rôles DB dédiés. Elles restent temporairement dans `apps/streamlit-ui` jusqu'à ce remplacement.
3. **Aucune page active n'est archivée par A3.** Les seules archives sont `07_Eval_Comparison` et `11_Golden_Beta_Analysis`, déjà retirées et remplacées avant ce chantier ([motifs et remplacements](../../../apps/streamlit-ui/archive/README.md)).
4. **L'import de sources reste dans le domaine ingestion.** `15_Import_Sources` conserve ses accès Grist/S3 côté serveur ; cette exception ne donne aucun accès Postgres RAG au produit Streamlit.
5. **Les documents ont une route de lecture, pas une route d'énumération.** Les URLs publiques d'origine sont retournées directement. Un document interne cité reçoit une capability opaque et révocable, limitée à ce document et au run qui l'a cité.
6. **Les bearers Streamlit sont provisionnés côté serveur.** Un bundle secret associe chaque slug à un bearer propre. Aucun bearer n'entre dans un cookie, `st.session_state`, le navigateur, une URL, un log ou un artefact CI.

## Catégories de décision

- **Client HTTP** : la page reste dans le produit Streamlit et appelle uniquement les routes listées dans ce document.
- **Maintien temporaire** : l'accès direct actuel reste disponible uniquement pour le rollback ou jusqu'à livraison du remplacement nommé ; sa ligne fixe le gate de sortie E2 ou F3.
- **Outil séparé** : la fonction ne passe pas par l'API RAG publique ; elle appartient à RAG-ops ou au domaine ingestion, avec son propre contrôle d'accès. Elle peut partager temporairement le shell Streamlit existant.
- **Archivage approuvé** : la fonction a déjà un remplacement documenté ; aucune nouvelle perte n'est autorisée par A3.

## Rôles et frontières d'autorisation

| Rôle | Authentification | Accès permis | Interdictions et données sensibles |
|---|---|---|---|
| Probe | aucune | `GET /healthz` | Aucun détail de secret, DSN, provider ou corpus. |
| Visiteur Streamlit | aucune | catalogue des groupes visibles et vérification d'un mot de passe | Réponses génériques, rate limiting obligatoire, mot de passe jamais logué. Aucun bearer n'est retourné. |
| Groupe | bearer opaque propre au groupe | modèles autorisés, chat, feedback de ses runs, document autorisé | Ministères filtrés par la politique du groupe ; un run hors groupe répond 404. Questions, réponses et feedback ne sont pas lisibles par les routes publiques. |
| Administrateur | bearer d'un groupe `is_admin=true` | toute la surface `/admin/*` | Questions/réponses, sessions, traces, prompts et gold answers sont sensibles. Accès audité ; aucun hash de mot de passe/token n'est retourné. Le rôle admin est revérifié à chaque requête. |
| RAG-ops | identité d'opérateur + réseau restreint + rôle DB dédié | corpus en lecture ; goldset et tables d'expériences selon le sous-rôle | Aucun bearer public ne donne cet accès. Pas de DDL au runtime ; pas de secret provider dans les résultats exportés. |
| Ingestion | admin Streamlit + secrets Grist/S3 côté serveur | dépôt de documents et mise à jour du manifeste | Aucun accès Postgres RAG. Contenu uploadé, clés S3 et clé Grist restent côté serveur. |

Un administrateur conserve la visibilité fonctionnelle actuelle sur tous les groupes et ministères pour les routes admin. Cette capacité globale est explicite, auditée et distincte de `allowed_ministries`, qui borne les routes publiques du groupe.

## Matrice page par page

| Page / fonction actuelle | Décision cible | Remplacement figé | Autorisation / données | Transition et propriétaire |
|---|---|---|---|---|
| `Home.py` — groupes visibles, login par mot de passe, cookie de groupe | **Client HTTP** | `GET /v1/auth/groups`, `POST /v1/auth/verify`, `GET /v1/auth/me` | Visiteur ; mot de passe write-only, bearer absent de la réponse. | B4/D2 livrent l'auth ; E1 migre le client. DB direct maintenu seulement pour rollback jusqu'à F3. |
| `01_Chatbot` — modèles/ministères, historique cinq tours, stream, sources | **Client HTTP** | `GET /v1/models`, `POST /v1/chat/completions`, route documentaire | Bearer de groupe ; corpus borné à `allowed_ministries`. | C1–C7 puis E1. `RAG_CHAT_BACKEND=direct|api` jusqu'à F3. |
| `01_Chatbot` — feedback structuré | **Client HTTP** | `POST /v1/feedback` | Bearer du groupe propriétaire ; raisons/commentaire potentiellement personnels. | D1 puis E1 ; ownership groupe/run obligatoire. |
| `02_Chat_Logs` — filtres, tableau, détail, métriques pipeline | **Client HTTP** | liste/détail/stats de `chat-runs` | Admin ; questions, réponses, ids session et prompts/traces sensibles. | D2/E2. Le fallback CSV local rejoint RAG-ops ; il n'est pas un fallback de production. |
| `03_Feedback_Dashboard` — filtres, métriques, raisons, usage, exports XLSX | **Client HTTP** | `feedback`, `feedback/stats` et `chat-runs/stats`; XLSX construit côté client | Admin ; feedback, sessions, questions/réponses sensibles. | D2/E2. Les routes restent paginées ; l'export client parcourt les pages avec une borne explicite. |
| `03_Feedback_Dashboard` — analyse IA des négatifs | **Client HTTP** | création et suivi d'un job `feedback/analyze` | Admin ; pas de clé provider ni prompt secret dans le statut. | D1/D2. Claim idempotent côté serveur ; aucun traitement long attaché à un rerun Streamlit. |
| `04_Admin_Config` — lecture, édition et reset de la config | **Client HTTP** | `rag-config` + action `reset` | Admin ; paramètres globaux, mutation CAS et audit. | D2/E2. |
| `04_Admin_Config` — liste, création, édition, duplication, suppression des prompts | **Client HTTP** | routes `system-prompts` explicites | Admin ; contenu de prompt interne, actif/par défaut protégé. | D2/E2. |
| `04_Admin_Config` — CRUD acronymes, catégories, manquants et marquage traité | **Client HTTP** | routes `acronyms` explicites | Admin ; révision incrémentée à chaque mutation. | D2/E2. |
| `04_Admin_Config` — helper health providers inutilisé | **Archivage approuvé** | `/healthz` pour le service ; observabilité pour les providers | Probe synthétique ; aucune clé ou réponse provider. | Suppression lors d'E2 : ce helper n'est appelé par aucun parcours UI. Aucun endpoint de test de secret/provider n'est créé. |
| `05_DB_Explorer` — stats, documents, chunks et sections | **Outil séparé** | vue lecture seule de RAG-ops, sans API SQL | RAG-ops reader ; contenu intégral du corpus. | D3 livre le lecteur sur `ContentStorePort`; E2 retire la page publique. Maintien temporaire jusque-là. Propriétaire : API/RAG-ops. |
| `06_Goldset_Explorer` — filtres, exports, tags, difficulté, goldset name, stats | **Outil séparé** | vue de curation RAG-ops, fonctions unitaires et batch conservées | `rag_quality_editor` ; questions et gold answers internes, mutations auditées. | D3 livre le remplacement ; E2 retire la page publique. |
| `08_Chunking_Evaluation` — stratégies, tournoi, sauvegarde/chargement, détail | **Outil séparé** | runner + vue RAG-ops `chunking` | RAG-ops ; lecture corpus/goldset, écriture limitée aux expériences. | D3, avant retrait E2. |
| `09_Pipeline_Evaluation` — ablations, latences, sauvegarde/chargement/suppression, CSV | **Outil séparé** | runner + vue RAG-ops `pipeline-ablation` | RAG-ops ; résultats d'expériences, aucun secret provider exporté. | D3, avant retrait E2. Utilise le nouveau core par port public, jamais ses attributs privés. |
| `10_Intent_Gater_Evaluation` — corpus intent, imports, run, manuel, expériences | **Outil séparé** | runner + vue RAG-ops `intent` | RAG-ops ; questions de chats importées potentiellement personnelles. | D3, avant retrait E2. Les tables sont créées par migration, jamais par la page. |
| `12_Pipeline_Timeline` — sélection de run, détail et événements ordonnés | **Client HTTP** | `chat-runs`, détail et `trace` | Admin ; payloads de trace sensibles et volumineux. | D2/E2. Payloads lourds uniquement au détail, jamais dans la liste. |
| `13_admin` — raccourci vers l'administration | **Client HTTP** | redirection locale vers `04_Admin_Config` après `GET /v1/auth/me` | Bearer admin ; aucune donnée propre. | E2. Aucun endpoint propre. |
| `14_User_Groups` — liste, création, édition, reset mot de passe, suppression protégée | **Client HTTP** | CRUD `user-groups` + `reset-password` | Admin ; mots de passe write-only, hashes jamais retournés. | B2/B4/D2 puis E2. Invariants serveur décrits plus bas. |
| `14_User_Groups` — émission/rotation/révocation de bearer | **Client HTTP** | collection `user-groups/{slug}/tokens` | Admin ; bearer retourné une fois, métadonnées seules ensuite. | B2/B4/D2. Rotation en deux phases. |
| `15_Import_Sources` — PDF dropzone et références juridiques | **Outil séparé** | domaine ingestion existant Grist/S3, dans une frontière explicitement exemptée | Admin Streamlit ; documents et secrets Grist/S3 côté serveur. | Inchangé. Propriétaire : data engineering. L'auth admin Streamlit passe toutefois par HTTP en E2. |
| `_PDF_Viewer` — DB legacy, dropzone S3, redirection externe | **Client HTTP** | `GET /v1/documents/{doc_ref}/content` ou URL publique d'origine | Bearer de groupe ou capability d'un document cité. | B2/D2/E1. Plus aucun accès DB/S3 depuis le viewer produit. |
| `archive/07_Eval_Comparison` | **Archivage approuvé** | `rag_quality_eval_runs` + journal/runner d'éval | RAG-ops ; aucun endpoint public/admin. | Décision antérieure conservée. |
| `archive/11_Golden_Beta_Analysis` | **Archivage approuvé** | `03_Feedback_Dashboard` | Admin via les routes feedback. | Décision antérieure conservée. |

### Gate de retrait des pages RAG-ops

Les pages `05`, `06`, `08`, `09` et `10` ne quittent `apps/streamlit-ui` qu'après un test de parité fonctionnelle du remplacement. Le test couvre au minimum : mêmes filtres et champs utiles, mêmes mutations, import/export, sauvegarde/chargement/suppression des expériences et mêmes calculs déterministes sur un fixture. Si ce gate échoue, la page reste en maintien temporaire ; elle ne justifie ni un endpoint SQL générique ni une suppression de fonction.

## Registre figé des endpoints publics et documentaires

| Endpoint | Livraison | Autorisation | Données et règle de sécurité | Propriétaire |
|---|---|---|---|---|
| `GET /healthz` | A4, existant | aucune | État synthétique uniquement, aucun nom de secret/provider. | API/ops |
| `GET /v1/auth/groups` | D2 | aucune | Seulement `slug`, label, icône et couleur des groupes visibles, non-admin et loginables. | API/auth |
| `POST /v1/auth/verify` | D2 | aucune, rate-limitée | Reçoit slug/mot de passe, retourne métadonnées + `credential_revision`, jamais de bearer. 401 identique pour groupe absent/mot de passe faux. | API/auth |
| `GET /v1/auth/me` | B4 | bearer de groupe | Slug, rôle, politiques ministères et révisions non secrètes ; sert au fail-closed Streamlit. | API/auth |
| `GET /v1/models` | B5 | bearer de groupe | Catalogue filtré par `allowed_ministries`. | API/chat |
| `POST /v1/chat/completions` | C1–C7 | bearer de groupe | Question, historique, réponse et sources ; run en UUID complet, pas de secret dans traces. | API/chat |
| `POST /v1/feedback` | D1 | bearer propriétaire du run | 404 pour run absent **ou hors groupe** ; raisons structurées ; upsert audité. | API/feedback |
| `GET /v1/documents/{doc_ref}/content` | D2 | bearer autorisé **ou** capability de source | Aucune liste. Résout DB legacy, objet S3 ou URL externe sans exposer clé de stockage. | API/documents |

`GET /v1/auth/groups` et `POST /v1/auth/verify` sont destinés au backend Streamlit. CORS n'est pas ouvert et leur appel direct depuis un navigateur ne crée aucune session API. La vérification de mot de passe est protégée par quotas IP + slug, backoff et métriques sans label de mot de passe.

### Accès documentaire étroit

- Une source avec URL publique canonique utilise cette URL directement.
- Pour un document interne, le service crée dans le bloc source une URL absolue vers `GET /v1/documents/{doc_ref}/content` avec une capability opaque signée sur `doc_ref + turn_id`.
- La capability n'autorise ni listing ni autre document. Le serveur vérifie que le document figure dans les sources persistées du run et que ce run a été produit pour un groupe autorisé. Elle reste valide pendant la rétention du run, et peut être révoquée en supprimant l'association ou en tournant la clé de signature.
- La possession de l'URL donne lecture du document cité : elle est donc traitée comme donnée sensible. Le paramètre est masqué dans les logs, `Referrer-Policy: no-referrer` est envoyé et aucune clé S3, UUID legacy ou bearer de groupe n'est incorporé.
- Avec un bearer, la route exige que le document appartienne à un corpus autorisé par le groupe. Les échecs d'existence et d'autorisation répondent tous deux 404.

## Registre figé des endpoints admin D2

Toutes ces routes exigent `is_admin=true`, utilisent pagination/bornes, produisent un événement d'audit pour les mutations et ne retournent jamais `password_hash`, `secret_hash`, DSN ou clé provider.

| Ressource | Endpoints exacts | Fonction conservée | Données sensibles / protections | Propriétaire |
|---|---|---|---|---|
| Config | `GET /admin/rag-config`; `PUT /admin/rag-config`; `POST /admin/rag-config/reset` | lecture, mise à jour partielle, reset défauts | `If-Match`/révision obligatoire aux mutations ; reset explicite et audité. | API/admin D2 |
| Prompts | `GET /admin/system-prompts`; `POST /admin/system-prompts`; `GET /admin/system-prompts/{name}`; `PUT /admin/system-prompts/{name}`; `POST /admin/system-prompts/{name}/duplicate`; `DELETE /admin/system-prompts/{name}` | liste, détail, création, édition, duplication, suppression | Contenu interne ; nom allowlisté ; prompt actif/par défaut non supprimable ; CAS. | API/admin D2 |
| Acronymes | `GET /admin/acronyms`; `POST /admin/acronyms`; `PUT /admin/acronyms/{acronym}`; `DELETE /admin/acronyms/{acronym}`; `GET /admin/acronyms/missing`; `POST /admin/acronyms/missing/{acronym}/resolve` | CRUD, recherche/catégories, manquants, marquage traité | Entrées normalisées, limites de taille, révision et audit. | API/admin D2 |
| Groupes | `GET /admin/user-groups`; `POST /admin/user-groups`; `PATCH /admin/user-groups/{slug}`; `DELETE /admin/user-groups/{slug}`; `POST /admin/user-groups/{slug}/reset-password` | CRUD complet et reset | Mot de passe write-only ; groupes structurels et dernier admin non supprimables ; `default` jamais admin ; suppression révoque les tokens. | API/auth D2 |
| Bearers | `GET /admin/user-groups/{slug}/tokens`; `POST /admin/user-groups/{slug}/tokens`; `DELETE /admin/user-groups/{slug}/tokens/{token_id}` | inventaire, émission, rotation, révocation | Liste = métadonnées seulement ; secret affiché une fois ; identifiant indexé ; ancien token conservé jusqu'à révocation explicite. | API/auth B4/D2 |
| Runs | `GET /admin/chat-runs`; `GET /admin/chat-runs/stats`; `GET /admin/chat-runs/{turn_id}`; `GET /admin/chat-runs/{turn_id}/trace` | logs, métriques, détail, timeline | Questions, réponses, ids de session, prompts/traces ; liste résumée, détail à la demande, filtres bornés. | API/observabilité D2 |
| Feedback | `GET /admin/feedback`; `GET /admin/feedback/stats`; `POST /admin/feedback/analyze`; `GET /admin/feedback/analyze/{job_id}` | dashboard, stats, lancement et suivi analyse | Commentaires et réponses potentiellement personnelles ; job claimé/idempotent ; résultats paginés. | API/feedback D1/D2 |

Sont explicitement absents de la v1 : endpoint SQL, endpoint corpus/chunks/sections, endpoint goldset, endpoint d'ablation/intent/chunking et exécution arbitraire de job. Ces fonctions appartiennent à RAG-ops.

## Opérations sensibles et invariants serveur

### Feedback

- Le `turn_id` est un UUID complet et l'ownership est vérifié sur le groupe du bearer.
- Un accès hors groupe est indiscernable d'un id inconnu (404).
- L'API expose `stars` en 1–5 mais, pendant la coexistence, l'adaptateur persiste 0–4 afin de ne pas mélanger deux encodages avec l'ancien runtime. Les lectures API normalisent en 1–5. Une migration atomique ultérieure pourra changer le stockage seulement après retrait de tous les consommateurs 0–4.

### Mot de passe et suppression de groupe

- Création et reset acceptent le mot de passe en écriture seulement. Il est exclu des logs, traces, erreurs et réponses.
- Le reset incrémente `credential_revision`; les cookies Streamlit portent cette révision et sont invalidés si `GET /v1/auth/me` annonce une révision différente.
- `DELETE` refuse tous les slugs structurels, le dernier groupe admin et toute révision obsolète. Il révoque les tokens dans la même transaction et conserve les libellés historiques déjà copiés dans les runs/audits.

### Rotation de bearer

- Le format est `arh_<env>_<token_id>.<secret>`. `token_id` est non secret et indexé ; seul le secret subit la vérification PBKDF2 en temps constant.
- Les tokens vivent dans `user_group_api_tokens` (`token_id`, `group_slug`, `secret_hash`, label, dates, révocation), pas dans un unique `user_groups.api_token_hash`. Plusieurs tokens actifs permettent une rotation sans coupure.
- `POST .../tokens` crée un token et retourne son clair une seule fois. `DELETE .../tokens/{token_id}` le révoque. Ni GET, ni backup, ni log ne permettent de récupérer le clair.
- Le bootstrap du premier admin est une commande serveur : elle persiste le hash, imprime le bearer une fois sur le terminal opérateur et n'écrit aucun fichier.

### Config, prompts et acronymes

- Les mutations utilisent une révision attendue (`If-Match` ou champ équivalent) et échouent en 409 sur concurrence.
- Le reset de config, la suppression d'un prompt et la suppression d'un acronyme sont audités avec acteur, ressource, révision avant/après et horodatage, jamais avec le contenu secret d'une requête.

## Provisioning serveur des bearers Streamlit

Le mode retenu pour E1 est un **bundle JSON injecté comme variable d'environnement secrète Scaleway**, cohérent avec le déploiement actuel via `secret-environment-variables` :

```json
{
  "version": 1,
  "tokens": {
    "dgafp-beta": "arh_prod_<token_id>.<secret>",
    "dgafpallianceadmin": "arh_prod_<token_id>.<secret>"
  }
}
```

- Nom : `STREAMLIT_API_BEARERS_JSON`; `ASSISTANT_RH_API_URL` reste une variable non secrète.
- Le bundle est lu une fois dans un cache serveur privé et validé via `GET /v1/auth/me`. Une entrée absente, malformée ou associée au mauvais slug fait échouer la requête fermée ; aucun fallback vers un bearer admin n'existe.
- Le navigateur ne reçoit que le cookie Streamlit chiffré `{slug, credential_revision}`. Le backend choisit le bearer par slug après authentification ; un paramètre de requête ne peut pas choisir un token admin.
- Le déploiement et ses erreurs redigent chaque valeur du bundle. Les probes, captures A2, traces et exports ne contiennent jamais `Authorization` ni le JSON secret.

Rotation sans interruption :

1. émettre un nouveau token labellisé `streamlit` avec `POST .../tokens` ;
2. remplacer le bundle secret du container et redéployer/recharger Streamlit ;
3. valider `GET /v1/auth/me` et `GET /v1/models` pour chaque slug configuré ;
4. révoquer l'ancien `token_id` seulement après le smoke ;
5. en cas d'échec avant l'étape 4, restaurer le bundle précédent, encore valide.

Le bearer unique d'une configuration provider `conversations` constaté par A2 reste un sujet du fork temps 2 ; il n'est pas réutilisé comme bundle multi-groupes Streamlit.

## Pertes fonctionnelles et conditions d'acceptation

- **Nouvelle perte fonctionnelle : aucune ; aucune approbation de perte n'est donc requise.** Les pages actives restent soit clientes HTTP, soit disponibles dans un outil séparé avant leur retrait du produit public.
- Le déplacement RAG-ops change l'URL et le mode d'accès, pas les capacités. Cette contrainte est vérifiée par le gate de parité plus haut.
- Les deux pages déjà archivées restent couvertes par leurs remplacements documentés ; A3 ne rouvre pas leur schéma obsolète.
- Toute proposition ultérieure de supprimer une fonction, de réduire un export ou de ne pas migrer une mutation exige une validation produit distincte consignée dans le LEDGER avant E2.

La matrice est réputée satisfaite lorsque les tests D1/D2 couvrent les autorisations de ce registre, que D3 couvre le gate RAG-ops et qu'E1 prouve le provisioning/rotation sans exposition navigateur.
