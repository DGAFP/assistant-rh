# Recommandation — Backend Assistant RH & intégration ProConnect

> **Statut historique.** Cette recommandation a été remplacée par le plan
> [`hexagonal-split`](../architecture/hexagonal-split/00-overview.md). Les
> références au candidat TypeScript supprimé par [#440](https://github.com/DGAFP/assistant-rh/issues/440)
> sont conservées uniquement comme trace de la décision de juin 2026.

> Date : 2026-06-17.  
> Objet : synthèse opérationnelle pour décider **comment implémenter le backend produit** et **où intégrer ProConnect**, en tenant compte de l'état actuel du repo, des audits existants et des audits ouverts en PR [#135](https://github.com/DGAFP/assistant-rh/pull/135), [#136](https://github.com/DGAFP/assistant-rh/pull/136), [#137](https://github.com/DGAFP/assistant-rh/pull/137).

---

## 0. Verdict court

**Ne pas construire un “backend” générique ni greffer ProConnect dans Streamlit.**

La bonne trajectoire est :

1. **Tracer une frontière HTTP stable autour du RAG Python actuel**, via un backend mince exposant un contrat OpenAI-compatible (`/v1/chat/completions`, SSE, citations/metadata documentées).
2. **Mettre ProConnect dans une vraie couche UI/BFF** — idéalement `suitenumerique/conversations` si le spike confirme la faisabilité, sinon LibreChat ou un BFF custom — et garder l'API RAG comme service interne protégé par service-token / réseau privé.
3. **Garder Streamlit comme outil interne transitoire** (admin, debug, évaluation), pas comme surface publique SSO.
4. **Ne pas relancer Mastra comme P0** : le port Mastra reste utile comme backend alternatif futur parce qu'il parle déjà une forme OpenAI-compatible, mais la note RAG en PR #136 confirme que le pipeline Python actuel est sain ; la priorité est donc l'extraction de frontière, pas la réécriture.

Architecture cible recommandée :

```text
Agent public / RH
  → UI/BFF chat avec ProConnect
      - session applicative
      - utilisateurs, rôles, groupes, historique
      - RGPD / consentement / logout
  → Assistant RH RAG API interne
      - /v1/chat/completions
      - auth service-to-service uniquement
      - logs RAG, traces, citations
  → Postgres + Albert/DINUM + Scaleway fallback
```

---

## 1. Inputs pris en compte

### 1.1 PR #135 — audit UI Streamlit & frontière UI ↔ pipeline

Constat structurant : l'UI Streamlit importe le pipeline en process et lit directement ses internes (`last_result.context_items`, timing, metadata). L'historique vit dans `st.session_state`, lié au websocket, donc fragile derrière reverse-proxy / SSO.

Implication pour ce plan :

- **La première brique backend n'est pas ProConnect : c'est la frontière HTTP UI ↔ RAG.**
- **Streamlit ne doit pas devenir le réceptacle de l'OIDC long terme.** Même si un proxy OIDC peut protéger l'existant tactiquement, ce serait une impasse produit.
- Le contrat doit permettre plusieurs UIs : Streamlit aminci, `suitenumerique/conversations`, LibreChat, ou une UI Next.js/DSFR.

### 1.2 PR #136 — revue RAG vs état de l'art

Constat structurant : le pipeline RAG Python est architecturalement sain et au-dessus de la moyenne ; pas de rewrite framework recommandé. Les améliorations importantes sont incrémentales : goldset, vérificateur post-génération, Contextual Retrieval/SAC, observabilité OpenInference/Langfuse, etc.

Implication pour ce plan :

- **Le backend P0 doit wrapper le pipeline Python existant**, pas le remplacer.
- L'API doit exposer assez de diagnostics pour ne pas perdre les métriques RAG actuelles, mais ne doit pas figer les internes Python comme contrat public.
- Mastra doit rester un backend substituable derrière le même contrat, pas un prérequis à ProConnect.

### 1.3 PR #137 — audit ingestion

Constat structurant : l'ingestion a des bases saines mais doit évoluer : Contextual Retrieval, chunking article-level Légifrance, tracking incrémental, versioning embeddings, quality gate RAGAS.

Implication pour ce plan :

- **Ne pas bloquer l'auth / backend sur la refonte ingestion.** Les améliorations ingestion changent la qualité des réponses, pas la frontière produit.
- Prévoir dans l'API des champs de metadata compatibles avec le futur versioning d'embeddings / ingestion (`source`, `document_id`, `chunk_id`, `embedding_model_version`, `ingestion_run_id` quand disponible), sans exiger qu'ils existent dès le premier sprint.
- Les quality gates RAGAS/goldset deviendront la protection de régression du backend une fois l'API en place.

### 1.4 État actuel du repo

- Production actuelle : Streamlit (`apps/streamlit-ui/`) appelle le package Python `packages/rag-pipeline/` en process.
- API existante mais non active : `apps/mastra-pipeline/` expose déjà `/v1/chat/completions` et `/v1/models`, sans être la voie de prod ni le bon endroit P0 pour ProConnect.
- Auth actuelle : cookies chiffrés + password admin (`src/ui/admin_auth.py`, `src/ui/cookies_security.py`), groupes via cookie / `?group=` pour les cohortes non-admin. C'est acceptable pour beta/admin, pas pour une identité gouvernementale.
- Docs historiques : `docs/architecture/UI_REPLACEMENT_ANALYSIS.md` et `docs/mastra/MASTRA_PORT_EXECUTIVE_SUMMARY.md` recommandaient déjà `suitenumerique/conversations` avec LibreChat en fallback.

---

## 2. Décision d'architecture recommandée

### 2.1 Créer une API RAG interne, pas un BFF monolithique

Le backend Assistant RH doit être découpé en deux responsabilités :

| Couche | Responsabilité | Auth | Pourquoi |
|---|---|---|---|
| **UI/BFF** | ProConnect, session utilisateur, historique conversationnel, rôles, UX chat, logout | OIDC utilisateur | C'est une couche web produit ; elle gère cookies, CSRF, consentement, RBAC. |
| **RAG API** | Répondre à une requête RAG, streamer la réponse, renvoyer citations/diagnostics | Service-to-service | C'est une couche moteur ; elle ne doit pas stocker ni manipuler les tokens ProConnect. |

À éviter :

- exposer directement l'API RAG à Internet avec des tokens ProConnect en Bearer ;
- faire de Streamlit le callback OIDC principal ;
- mettre les rôles métier dans le query string (`?group=`) ;
- coupler le contrat API à `PipelineResult` / `last_result`.

### 2.2 Backend RAG P0 : FastAPI mince autour du pipeline Python

Recommandation : créer un nouveau service Python, par exemple :

```text
apps/rag-api/
  src/assistant_rh_rag_api/
    main.py
    auth.py              # service-token, pas ProConnect utilisateur
    openai_schema.py     # request/response/SSE
    pipeline_adapter.py  # RAGConfig + PipelineResult → contrat API
    observability.py
```

Pourquoi FastAPI maintenant :

- le pipeline de production est Python ;
- PR #136 déconseille une réécriture ;
- on peut livrer une frontière HTTP sans modifier l'algorithme RAG ;
- Mastra pourra ensuite implémenter le même contrat si le port reprend.

Contrat minimal :

```text
GET  /healthz
GET  /readyz
GET  /v1/models
POST /v1/chat/completions
```

Exigences de contrat :

- requête compatible OpenAI Chat Completions (`messages`, `stream`, `temperature` optionnelle) ;
- SSE si `stream=true` ;
- réponse non-stream compatible OpenAI pour les clients simples ;
- extensions documentées pour les citations, diagnostics RAG et statut des étapes ;
- `conversation_id`, `user_context`, `group_context` acceptés comme metadata applicative **venant du BFF**, pas du navigateur directement ;
- aucune dépendance du client aux classes internes Python.

Exemple conceptuel :

```json
{
  "model": "assistant-rh-rag-v3",
  "messages": [
    {"role": "user", "content": "Quels sont mes droits au renouvellement de CDD ?"}
  ],
  "stream": true,
  "metadata": {
    "conversation_id": "...",
    "app_user_id": "...",
    "group": "dgafpsd1"
  }
}
```

La RAG API doit rejeter `metadata.group` si l'appel ne vient pas d'un client de confiance. Le navigateur ne doit jamais pouvoir imposer directement son groupe.

### 2.3 ProConnect : dans le BFF, jamais dans le moteur RAG

ProConnect doit être implémenté côté fournisseur de service (FS) avec l'Authorization Code Flow OIDC :

- découverte via `https://PROCONNECT_DOMAIN/api/v2/.well-known/openid-configuration` ;
- `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `end_session_endpoint` ;
- génération et vérification de `state` et `nonce` ;
- callback exact `redirect_uri` déclaré ;
- échange `code` → tokens en back-channel avec `client_secret` ;
- vérification de signature de l'`id_token` et du `userinfo` JWT ;
- logout avec `id_token_hint` et `post_logout_redirect_uri`.

Données utiles côté ProConnect d'après la documentation FS :

| Scope | Claim |
|---|---|
| `openid` | `sub` |
| `given_name` | `given_name` |
| `usual_name` | `usual_name` |
| `email` | `email` |
| `uid` | `uid` |
| `siret` | `siret` |
| `idp_id` | `idp_id` |
| `phone` optionnel | `phone_number` |
| `custom` optionnel | `custom` |

Points de vigilance :

- le `code` d'autorisation expire vite ; la doc ProConnect indique 30 secondes ;
- l'`access_token` est court ; la doc indique 60 secondes ;
- la session ProConnect est de 12h ; la session applicative peut être plus courte, mais il faut assumer le silent login éventuel ;
- ProConnect authentifie l'agent, **il ne décide pas seul de ses habilitations Assistant RH**.

---

## 3. Choix UI/BFF

### Option A — `suitenumerique/conversations` comme UI/BFF cible

**Recommandation par défaut : spike en premier.**

Raisons :

- alignement écosystème DINUM / La Suite numérique ;
- OIDC / ProConnect déjà dans l'orientation produit ;
- stack Next.js + backend Django + Vercel AI SDK, cohérente avec les constats de PR #135 ;
- connexion à un endpoint OpenAI-compatible via `AI_BASE_URL` prévue par les analyses existantes.

Risques à valider en spike :

- maturité et stabilité des versions ;
- compatibilité réelle avec le déploiement Scaleway visé ;
- capacité à afficher correctement les citations et disclaimers Assistant RH ;
- modèle de rôles suffisant pour les groupes métier / admin ;
- effort de personnalisation DSFR / wording RH.

Critère de succès du spike :

- login ProConnect test fonctionnel ;
- appel à une RAG API de test OpenAI-compatible ;
- streaming affiché ;
- historique persisté ;
- citation ou metadata au moins affichable ;
- logout complet ;
- déploiement staging reproductible.

### Option B — LibreChat comme fallback robuste

À choisir si `conversations` est trop jeune ou trop coûteux à déployer.

Avantages : maturité, OIDC/Keycloak, Docker, communauté.  
Inconvénients : moins aligné DINUM, moins DSFR, adaptation UX probablement plus forte.

### Option C — BFF custom Next.js/DSFR

À réserver si A et B bloquent.

Ce serait le meilleur contrôle produit, mais le plus gros coût : composants DSFR, auth, sessions, historique, admin, streaming, citations, accessibilité, tests e2e.

### Option D — Streamlit + proxy OIDC

Acceptable seulement comme mesure tactique de protection d'un pilote existant.

À ne pas choisir comme architecture cible : PR #135 montre que les limites Streamlit sont structurelles pour un produit SSO derrière reverse-proxy.

---

## 4. Modèle d'identité et d'autorisation

### 4.1 Séparer identité, habilitation et cohorte

ProConnect donne une identité. Assistant RH doit ensuite décider :

- utilisateur autorisé ou non ;
- rôle (`user`, `admin`, `evaluator`, `ops`) ;
- groupe/cohorte (`dgafpsd1`, `mattecentrale`, `mattedreal`, `cisirh`, etc.) ;
- ministère / organisation / SIRET si nécessaire ;
- accès aux pages admin ou aux logs.

Le mécanisme actuel `?group=` + cookie doit être supprimé de la surface publique. Il peut rester uniquement comme outil de debug local temporaire.

### 4.2 Tables applicatives proposées

À placer dans le BFF si on adopte `conversations`, ou dans un mini service auth si BFF custom :

```sql
app_users(
  id uuid primary key,
  created_at timestamptz not null,
  last_login_at timestamptz,
  display_name text,
  email text,
  status text not null -- active | blocked | pending
)

app_identities(
  id uuid primary key,
  user_id uuid not null references app_users(id),
  provider text not null,        -- proconnect
  subject text not null,         -- sub
  uid text,
  siret text,
  idp_id text,
  claims jsonb not null,
  unique(provider, subject)
)

app_roles(
  user_id uuid not null references app_users(id),
  role text not null,
  granted_by text,
  granted_at timestamptz not null,
  primary key(user_id, role)
)

app_group_memberships(
  user_id uuid not null references app_users(id),
  group_code text not null,
  source text not null,          -- allowlist | claim | admin
  granted_at timestamptz not null,
  primary key(user_id, group_code)
)
```

Le BFF transforme ensuite cela en contexte minimal pour le RAG :

```json
{
  "app_user_id": "uuid-interne",
  "roles": ["user"],
  "group": "dgafpsd1",
  "organization": {"siret": "..."}
}
```

Le RAG API logge l'identifiant applicatif pseudonymisé, pas les tokens ni le JWT ProConnect complet.

---

## 5. Plan d'implémentation recommandé

### Phase 0 — ADR et spike de compatibilité (3-5 jours)

Livrables :

- ADR “Backend boundary + ProConnect placement”.
- Contrat `/v1/chat/completions` validé avec un client simple (`curl`, script Python, éventuellement `conversations`).
- Décision provisoire : `conversations` spike prioritaire, LibreChat fallback.
- Liste des `redirect_uri` / `post_logout_redirect_uri` à déclarer auprès de ProConnect pour staging et production.

Gates :

- aucun secret ProConnect dans le repo ;
- aucune promesse UI sans test de streaming ;
- aucun démarrage d'une réécriture Mastra pour cette phase.

### Phase 1 — RAG API Python interne (1-2 sprints)

Livrables :

- service `apps/rag-api/` FastAPI ;
- `/healthz`, `/readyz`, `/v1/models`, `/v1/chat/completions` ;
- SSE fonctionnel ;
- adapter stable `PipelineResult → API response` ;
- auth service-token (`Authorization: Bearer <RAG_API_TOKEN>` ou mTLS / réseau privé selon infra) ;
- déploiement staging privé ;
- logs corrélés par `request_id`, `conversation_id`, `app_user_id` pseudonymisé.

Gates :

- pas d'accès public non authentifié ;
- pas de dépendance client aux champs internes Python ;
- tests contractuels pour streaming et non-streaming ;
- comparaison réponse Streamlit vs API sur un petit set de requêtes.

### Phase 2 — UI/BFF + ProConnect en test (1-2 sprints)

Livrables :

- instance `conversations` ou LibreChat en staging ;
- client OIDC ProConnect test déclaré ;
- scopes minimaux : `openid given_name usual_name email uid siret idp_id` ;
- session applicative créée après vérification `state`, `nonce`, signature JWT ;
- logout ProConnect + suppression session applicative ;
- appel RAG API avec service-token côté serveur uniquement ;
- historique conversationnel persistant et rechargeable.

Gates :

- le navigateur ne voit jamais le service-token RAG ;
- le RAG API ne reçoit jamais les tokens ProConnect bruts ;
- l'utilisateur ne peut pas choisir son `group` côté client ;
- les erreurs OIDC sont observables sans exposer de secrets.

### Phase 3 — Autorisation, RGPD, audit (1 sprint)

Livrables :

- mapping rôles/groupes via allowlist ou table admin ;
- remplacement du modèle `?group=` par `app_group_memberships` ;
- politique de rétention des conversations et logs ;
- mécanisme d'export / suppression si requis ;
- bannière / disclaimer cohérents avec l'usage RH ;
- audit log des changements de rôles et groupes.

Gates :

- revue RGPD / sécurité ;
- admin non accessible aux utilisateurs simples ;
- suppression ou neutralisation du group spoofing public.

### Phase 4 — Rollout progressif (1 sprint)

Livrables :

- staging bout-en-bout ;
- beta contrôlée par allowlist ;
- dashboards usage / erreurs / latence ;
- plan de rollback vers Streamlit ;
- runbook incident OIDC / RAG API / provider LLM.

Gates :

- disponibilité RAG API mesurée ;
- latence acceptable ;
- qualité vérifiée sur goldset minimal ;
- aucune fuite de PII dans traces techniques.

### Phase 5 — Mastra ou autre backend alternatif (optionnel, plus tard)

Mastra redevient intéressant quand :

- le contrat API est stabilisé ;
- les P0 qualité RAG / ingestion sont sous contrôle ;
- l'équipe veut comparer workflow/observabilité TS vs Python ;
- les tests contractuels permettent de substituer Python/Mastra sans changer l'UI.

À ce moment, `apps/mastra-pipeline/` doit viser le même contrat que `apps/rag-api/`, pas imposer une nouvelle UI.

---

## 6. Backlog concret à créer

| Priorité | Ticket | Résultat attendu |
|---|---|---|
| P0 | ADR backend boundary + ProConnect placement | Décision actée : ProConnect dans BFF, RAG API interne. |
| P0 | Créer `apps/rag-api` FastAPI | Pipeline Python exposé en HTTP sans rewrite. |
| P0 | Définir contrat `/v1/chat/completions` + extensions citations | Tests contractuels et doc client. |
| P0 | Auth service-to-service RAG API | Aucun accès navigateur direct au moteur. |
| P0 | Spike `suitenumerique/conversations` + ProConnect test | Valider ou invalider l'option cible. |
| P1 | Modèle users / identities / roles / groups | Fin du group spoofing par URL/cookie. |
| P1 | Conversation persistence côté BFF | Résout la perte `st.session_state` / websocket. |
| P1 | Observabilité API : request_id, user pseudonymisé, stage timings | Prépare OpenInference/Langfuse et dashboards. |
| P1 | Politique RGPD : rétention, export, suppression, logs | Pré-requis production. |
| P2 | Évaluer LibreChat si `conversations` bloque | Fallback documenté. |
| P2 | Adapter Mastra au contrat stabilisé | Backend substituable, pas dépendance P0. |

---

## 7. Risques et arbitrages

### Produit

- **Risque : choisir une UI trop tôt.** Mitigation : la frontière OpenAI-compatible rend le choix réversible.
- **Risque : confondre authentification et habilitation.** Mitigation : tables de rôles/groupes propres à Assistant RH.
- **Risque : perdre les citations / disclaimers dans une UI générique.** Mitigation : spike avec cas de citation comme critère bloquant.

### Technique

- **Risque : wrapper FastAPI trop couplé aux internes Python.** Mitigation : adapter unique, tests contractuels, schéma versionné.
- **Risque : double logging Streamlit + API incohérent.** Mitigation : `request_id` / `conversation_id` communs ; Streamlit devient client ou reste outil interne.
- **Risque : Mastra diverge.** Mitigation : même contrat, même goldset, mêmes tests de compatibilité.

### Sécurité / RGPD

- **Risque : tokens ProConnect propagés au RAG.** Mitigation : interdiction explicite ; session BFF → contexte minimal pseudonymisé.
- **Risque : groupe imposé par le client.** Mitigation : groupe calculé côté BFF uniquement.
- **Risque : historique conversationnel sensible.** Mitigation : rétention courte par défaut, séparation logs techniques / contenu, droit suppression/export.

### Livraison

- **Risque : vouloir tout faire en même temps** (ProConnect, UI, Mastra, ingestion, observabilité). Mitigation : séquencer : frontière API → ProConnect/BFF → autorisation/RGPD → rollout → Mastra optionnel.
- **Risque : bloquer ProConnect sur les chantiers ingestion.** Mitigation : les deux se mesurent ensemble mais se livrent séparément.

---

## 8. Ce qu'il ne faut pas faire

1. **Ne pas intégrer ProConnect directement dans Streamlit comme cible long terme.** C'est acceptable derrière proxy pour protéger un pilote, pas pour construire le produit.
2. **Ne pas exposer `/v1/chat/completions` publiquement sans BFF.** Le moteur RAG doit rester un service interne.
3. **Ne pas transmettre les JWT / access tokens ProConnect au pipeline.** Le pipeline reçoit un contexte applicatif minimal.
4. **Ne pas relancer Mastra comme prérequis à l'auth.** Le pipeline Python est le chemin le plus court et le plus sûr.
5. **Ne pas investir dans deux UIs durables.** Une seule UI doit viser le contrat HTTP ; Streamlit devient interne ou temporaire.
6. **Ne pas figer `PipelineResult` comme API publique.** Le contrat doit être stable même si le pipeline évolue.

---

## 9. Sources et documents liés

- PR [#135 — Audit UI Streamlit & frontière UI ↔ pipeline](https://github.com/DGAFP/assistant-rh/pull/135).
- PR [#136 — Revue RAG vs état de l'art 2025-2026](https://github.com/DGAFP/assistant-rh/pull/136).
- PR [#137 — Audit ingestion vs références 2025/2026](https://github.com/DGAFP/assistant-rh/pull/137).
- [`docs/architecture/UI_REPLACEMENT_ANALYSIS.md`](../architecture/UI_REPLACEMENT_ANALYSIS.md) — comparaison historique `suitenumerique/conversations`, Open WebUI, LibreChat, etc.
- Documentation ProConnect FS : [`implementation_technique.md`](https://github.com/numerique-gouv/proconnect-documentation/blob/main/doc_fs/implementation_technique.md) et [`scope-claims.md`](https://github.com/numerique-gouv/proconnect-documentation/blob/main/doc_fs/scope-claims.md).

---

## 10. Recommandation finale

**Implémenter d'abord une RAG API Python interne, puis brancher ProConnect via une UI/BFF dédiée.**

Ordre recommandé :

1. **Semaine 1 :** ADR + contrat API + spike client simple.
2. **Semaines 2-3 :** `apps/rag-api` FastAPI, service-token, SSE, tests contractuels, staging privé.
3. **Semaines 3-5 :** spike `suitenumerique/conversations` + ProConnect test ; LibreChat en fallback si blocage.
4. **Semaines 5-6 :** modèle users/rôles/groupes, suppression du group spoofing public, historique persistant.
5. **Semaines 7+ :** rollout beta, observabilité, RGPD, puis seulement ensuite réévaluer Mastra comme backend substituable.

Cette trajectoire respecte les trois audits récents :

- PR #135 : frontière HTTP avant décision UI long terme ;
- PR #136 : préserver le pipeline Python sain, améliorer par incréments ;
- PR #137 : ne pas mélanger backend/auth avec refonte ingestion, mais préparer les metadata et quality gates.

Elle est aussi la plus réversible : si `conversations` fonctionne, on bénéficie de l'alignement DINUM/ProConnect ; s'il bloque, LibreChat ou une UI custom peuvent consommer la même API ; si Mastra reprend, il remplace le moteur derrière le même contrat sans refaire l'auth ni l'UI.
