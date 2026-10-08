# Contrat de délégation Conversations → API

> Contrat du 2026-10-07 pour [#596](https://github.com/DGAFP/assistant-rh/issues/596), côté API. Il précise la [note d'identité individuelle](09-conversations-individual-access.md) et applique le choix « confiance de service » du 6 octobre, **proposé à la revue DGAFP** (le DAT v0.4 le classe parmi les arbitrages à clôturer).
> Aucun déploiement ni changement de base staging/production n'est associé à ce document. L'intégration côté Conversations relève de [#595](https://github.com/DGAFP/assistant-rh/issues/595).

## Acteurs et identités

| Identité | Porteur | Vérifiée par |
|---|---|---|
| Service appelant | Backend Conversations, détenteur d'une clé privée Ed25519 | L'API, par signature sur une clé publique épinglée (`kid`) |
| Utilisateur | Identifiant stable de l'utilisateur Conversations (`sub`) | Conversations (ProConnect) ; l'API fait confiance au service authentifié |
| Habilitations | Modèles accordés à l'utilisateur et ministère de la conversation | Conversations, à chaque requête ; l'API borne la requête à ce périmètre |

L'API ne tient ni registre d'utilisateurs ni appartenance aux groupes. Un changement de droits dans Conversations s'applique sans synchronisation côté API.

## Mécanisme retenu : assertion signée par requête

Plutôt que mTLS ou un serveur d'autorisation OAuth, le backend signe **une assertion courte par appel** (JWS compact, [RFC 7515](https://www.rfc-editor.org/rfc/rfc7515)), transmise dans `Authorization: Bearer`. C'est le principe des *client credentials* par assertion ([RFC 7523](https://www.rfc-editor.org/rfc/rfc7523)), sans serveur d'autorisation à exploiter :

- l'authentification du service et le contenu de la délégation sont liés par la même signature : un intermédiaire ne peut pas modifier l'utilisateur ou le périmètre ;
- le mécanisme fonctionne derrière un proxy qui termine TLS, ce que mTLS n'autorise pas sans propagation de certificat ;
- l'API ne détient que des clés publiques : la compromission de sa configuration ne permet pas de forger d'assertion ;
- le client OpenAI de Conversations transmet l'assertion comme `api_key`, sans en-tête propriétaire.

```mermaid
sequenceDiagram
    participant U as Agent
    participant C as Backend Conversations
    participant A as API Assistant RH
    U->>C: Question (session ProConnect)
    C->>C: Vérifie admission et habilitations courantes
    C->>C: Signe l'assertion (sub, models, ministry, exp ≤ 120 s)
    C->>A: POST /v1/chat/completions, Bearer <assertion>
    A->>A: Vérifie kid épinglé, signature, iss, aud, exp
    A->>A: Modèle demandé ∈ périmètre délégué ?
    A-->>C: Réponse ; run persisté avec auteur = sub
```

### Profil exact

En-tête : `alg` = `EdDSA` obligatoire, `kid` connu obligatoire, `typ` facultatif et égal à `JWT`. Tout autre membre (`crit`, `jku`, `x5u`…) entraîne un refus. Les membres JSON dupliqués sont refusés pour qu'aucun lecteur ne voie un autre périmètre que celui vérifié.

| Claim | Règle |
|---|---|
| `iss` | Égal à `CONVERSATIONS_DELEGATION_ISSUER` |
| `aud` | Chaîne ou liste contenant `ASSISTANT_RH_API_AUDIENCE` (défaut `assistant-rh-api`) ; une audience distincte par environnement empêche le rejeu d'une assertion de staging en production |
| `iat`, `exp`, `nbf` | Entiers. `exp` strictement futur, `exp − iat` ≤ 120 s, `iat` et `nbf` au plus 30 s dans le futur (dérive d'horloge) |
| `jti` | 16 à 128 caractères URL-safe |
| `sub` | Identifiant utilisateur Conversations, UUID canonique en minuscules ; devient l'auteur immuable du run |
| `models` | Modèles accordés (`assistant-rh-<ministère>`), uniques, liste vide admise ; un modèle inconnu fait refuser l'assertion (pas de déduction en cas de dérive du catalogue) |
| `ministry` | Facultatif : ministère fixé à la création de la conversation, compris dans `models` |
| `audit_session` | Facultatif : pseudonyme de session HMAC-SHA-256 (64 hex) calculé par Conversations avec une clé dédiée ; exigé par les écritures de feedback ([#528](https://github.com/DGAFP/assistant-rh/issues/528)) |

Tout écart produit le même 401 `invalid_api_key`, sans motif renvoyé ni journalisé.

## Règles d'autorisation appliquées par l'API

| Situation | Résultat |
|---|---|
| Appel sans assertion valide, signée par une clé non épinglée ou révoquée, expirée ou d'une autre audience | 401, même si un identifiant utilisateur est fourni par ailleurs |
| `GET /v1/models` | Modèles délégués, restreints à `ministry` s'il est présent ; liste vide pour un utilisateur sans habilitation |
| Chat avec un modèle hors périmètre, ou hors du ministère de la conversation | 403 `ministry_forbidden`, avant toute I/O de configuration, de corpus ou de LLM |
| Alias `assistant-rh` | Résolu vers `ministry` ; sans `ministry`, 403 (aucun défaut implicite) |
| Run créé | `author_user_id = sub`, `user_group = '@conversations'` (hors de l'alphabet des slugs B4, donc jamais un groupe), ministère du run conservé |
| Lecture d'un run, de ses sources ou feedbacks | Auteur = `sub` **et** ministère du run dans le périmètre courant, quel que soit le groupe |
| `/v1/auth/me`, `DELETE /v1/auth/session` | 401 : une délégation n'est pas une session B4 |

Champs client (`user`, `metadata.*`) : jamais lus comme identité.

## Rotation, révocation et retrait d'habilitation

- **Rotation** : Conversations publie une nouvelle clé (`kid` daté) ; l'API accepte simultanément l'ancienne et la nouvelle pendant la bascule, puis l'ancienne est retirée de `CONVERSATIONS_DELEGATION_JWKS`.
- **Révocation** (décidé le 2026-10-07) : le retrait d'un `kid` prend effet au redéploiement de l'API, sans rechargement à chaud de la JWKS ; les assertions déjà émises avec cette clé expirent au plus tard 120 s après leur émission.
- **Retrait d'une habilitation** : Conversations n'inclut plus le modèle dans l'assertion suivante ; l'API refuse dès la **requête suivante** et masque les runs du ministère sans les supprimer. Ils redeviennent accessibles si l'habilitation est rétablie.
- **Streams en cours** : l'autorisation est évaluée au démarrage de la requête ; une réponse déjà commencée se termine. Aucune nouvelle question ni lecture n'est acceptée sans nouvelle assertion.
- **Téléchargements** : délivrés par le backend après contrôle à chaque accès ([#594](https://github.com/DGAFP/assistant-rh/issues/594)) ; les octets déjà transmis ne sont pas révocables.

## Réseau

L'API n'est joignable que depuis le backend Conversations, par réseau privé, sans exposition publique. Ce contrôle relève du déploiement et complète l'assertion : un appel qui atteindrait l'API par un autre chemin reste refusé faute de signature valide. La configuration réseau sera validée lors de la recette Compose ([#593](https://github.com/DGAFP/assistant-rh/issues/593)) puis de l'environnement cible.

## Journaux

Ni assertion, ni clé, ni jeton ProConnect, ni contenu de délégation ne sont journalisés. Seul le pseudonyme `audit_session` est persisté pour l'audit des feedbacks. La corrélation par identifiant de requête entre Conversations et l'API reste à livrer.

## Risques acceptés

- **Backend compromis** : un backend Conversations compromis peut agir au nom de n'importe quel utilisateur. Le risque est réduit par le réseau privé, la clé de signature dédiée et sa rotation. Une preuve utilisateur de bout en bout pourra s'ajouter sans changer le modèle d'auteur.
- **Rejeu dans la fenêtre** : risque **non accepté** (décision du 2026-10-07). L'API mémorisera chaque `jti` dans une table PostgreSQL partagée par les réplicas (insertion unique, purge après `exp`) ; une assertion déjà vue sera refusée. Tant que cette table n'est pas livrée, une assertion interceptée sur le réseau privé reste rejouable jusqu'à son expiration (≤ 120 s).

## Reste à faire pour clore #596

1. Validation DGAFP du choix « confiance de service », ou consignation de l'écart au DAT : voir la [note de revue](15-revue-dgafp-confiance-service.md).
2. Côté Conversations ([#595](https://github.com/DGAFP/assistant-rh/issues/595)) : admission sur invitation, habilitations ministérielles, catalogue filtré, signature des assertions, pseudonyme d'audit, conservation de la clé privée côté serveur.
3. Vérifier la stabilité de l'identifiant utilisateur Conversations (jamais réattribué ; compte supprimé puis recréé).
4. Migration versionnée du schéma individuel (aujourd'hui local, `apps/api/sql/local/individual_feedback.sql`), table anti-rejeu des `jti` et restrictions SQL ([#599](https://github.com/DGAFP/assistant-rh/issues/599)) : prochaine tranche.
5. Corrélation par identifiant de requête et test de bout en bout connexion → catalogue → chat → run attribué depuis Conversations, avec reconnexion, expiration et révocation.
