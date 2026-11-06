# Contrat de délégation Conversations → API

> Contrat du 2026-10-07, réaligné le 2026-10-12 pour [#596](https://github.com/DGAFP/assistant-rh/issues/596). L’extrait validé du DAT communiqué le 12 octobre confirme la répartition Conversations/API, l’admission et le contrôle auteur + habilitations courantes. Il ne prescrit ni Ed25519 ni les durées exactes : ce document décrit le profil technique livré, dont les paramètres et la recette restent à consigner selon la [note de revue](15-revue-dgafp-confiance-service.md).
> Socle API fusionné dans `dev` (#597, #601, #602, #603), promotion staging suivie dans #611 ; aucun déploiement ou changement de base par ce document. Les adaptations d’identité dans `DGAFP/conversations` relèvent de #596, leur intégration produit et le feedback de [#595](https://github.com/DGAFP/assistant-rh/issues/595).

## Comprendre le parcours

Conversations identifie la personne et vérifie ses droits. À chaque appel, son serveur
envoie à l'API un message signé indiquant la personne et les ministères autorisés.
Ce message, appelé **assertion**, a une durée de vie maximale de deux minutes et ne peut
servir qu'une fois. L'API vérifie la signature et les droits avant de répondre.

Une réponse enregistrée en base est appelée un **run**. Elle conserve son auteur et
son ministère. Par exemple, Alice peut modifier son propre feedback tant qu'elle garde
accès au ministère concerné ; Bob ne le peut pas, même s'il travaille dans ce ministère.
Les anciennes conversations collectives restent collectives.

| PR | Ce qu'elle apporte |
|---|---|
| #597 | Enregistrer et remplacer un feedback sans perdre sa version précédente |
| #601 | Vérifier l'identité et les droits transmis par Conversations |
| #602 | Installer le schéma individuel et mémoriser les assertions déjà utilisées |
| #603 | Limiter les écritures SQL et sécuriser la purge contre les décalages d'horloge |

Le **rôle SQL** désigne les permissions d'une application sur la base ; il ne représente
pas l'utilisateur connecté. Les sections suivantes détaillent le contrat technique.

## Acteurs et identités

| Identité | Porteur | Vérifiée par |
|---|---|---|
| Service appelant | Backend Conversations, détenteur d'une clé privée Ed25519 | L'API, par signature sur une clé publique épinglée (`kid`) |
| Utilisateur | Identifiant stable de l'utilisateur Conversations (`sub`) | Conversations (ProConnect ou connexion locale temporaire) ; l'API fait confiance au service authentifié |
| Habilitations | Modèles accordés à l'utilisateur et ministère de la conversation | Conversations, à chaque requête ; l'API borne la requête à ce périmètre |

L'API ne tient ni registre d'utilisateurs ni appartenance aux groupes. Un changement de droits dans Conversations s'applique sans synchronisation côté API.

Avant le raccordement OIDC, le fork propose un mode explicite
`ASSISTANT_RH_LOCAL_LOGIN_ENABLED=true` : compte local avec mot de passe, admission
DGAFP et habilitations identiques. L'UUID interne reste le `sub` délégué ; le mot
de passe reste dans Conversations. Le rattachement ultérieur à OIDC doit conserver
ce compte, sans fusion automatique par e-mail. Voir la
[recette des deux modes](16-conversations-local-recipe.md).

## Mécanisme retenu : assertion signée par requête

Le profil livré ne nécessite pas de choisir entre mTLS et OAuth : le backend signe **une assertion courte par tentative HTTP** (JWS compact, [RFC 7515](https://www.rfc-editor.org/rfc/rfc7515)), transmise dans `Authorization: Bearer`. Il s’agit d’un contrat de délégation direct, sans échange de jeton OAuth ni serveur d’autorisation :

- l'authentification du service et le contenu de la délégation sont liés par la même signature : un intermédiaire ne peut pas modifier l'utilisateur ou le périmètre ;
- le mécanisme fonctionne derrière un proxy qui termine TLS, ce que mTLS n'autorise pas sans propagation de certificat ;
- l'API ne détient que des clés publiques : la compromission de sa configuration ne permet pas de forger d'assertion ;
- l’authentification HTTP du client Conversations remplace `Authorization` juste avant chaque envoi ; les retries automatiques du SDK sont désactivés. Aucun bearer n’est figé dans `api_key`, ni partagé entre utilisateurs. Toute nouvelle tentative relit session et droits puis signe un nouveau `jti`. L’usage unique n’assure pas l’idempotence d’une question ; le feedback a son propre contrat transactionnel.

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
| `jti` | 16 à 128 caractères URL-safe, à usage unique (une nouvelle assertion par appel) |
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

Ni assertion, ni clé, ni jeton ProConnect, ni contenu de délégation ne sont journalisés. Seul le pseudonyme `audit_session` est persisté pour l’audit des feedbacks. Conversations génère un identifiant aléatoire par tentative (`X-Client-Request-Id`, 32 caractères hexadécimaux minuscules). L’API accepte une valeur unique de ce format, sinon en génère une ; elle renvoie `X-Request-Id` et journalise cet identifiant avec le statut HTTP, sans contenu ni identité. La corrélation ne vaut jamais authentification. La recette cible doit vérifier la visibilité des deux journaux.

## Risques acceptés

- **Backend compromis** : un backend Conversations compromis peut agir au nom de n'importe quel utilisateur. Le risque est réduit par le réseau privé, la clé de signature dédiée et sa rotation. Une preuve utilisateur de bout en bout pourra s'ajouter sans changer le modèle d'auteur.
- **Rejeu dans la fenêtre** : risque **non accepté** (décision du 2026-10-07). L'API mémorise chaque `jti` vérifié dans `api_delegation_replays`, partagée par les réplicas (clé primaire, purge par lots après `exp`) ; une assertion déjà vue est refusée (401). Conversations signe donc une nouvelle assertion pour chaque appel, relances comprises.

La purge et le contrôle final d'expiration utilisent l'horloge PostgreSQL commune, indépendamment des horloges des réplicas API. Une assertion expirée en base reste refusée après purge de son `jti`, y compris si une attente sur un verrou SQL fait franchir l'expiration.

## Reste à faire pour clore #596

1. Consigner la déclinaison technique du DAT (profil Ed25519, durées, rotation et opérations en cours) et toute validation complémentaire requise, sans rouvrir les principes déjà confirmés : [note de revue](15-revue-dgafp-confiance-service.md).
2. Intégrer les adaptations #596 dans `DGAFP/conversations` : admission sur invitation ou validation centrale DGAFP, groupes multi-appartenance, modèles par groupe, catalogue filtré, ministère fixe et historique masqué/rétabli, signature et audit de session côté serveur. #595 raccorde le feedback et les messages aux complétions ; #594 raccorde les documents protégés.
3. Vérifier la stabilité de l'identifiant utilisateur Conversations (jamais réattribué ; compte supprimé puis recréé).
4. Restrictions SQL ([#599](https://github.com/DGAFP/assistant-rh/issues/599)) : rôles d'exécution non propriétaires et garde réservé à `arh_api` (`20261007130000_api_runtime_roles.sql`), à provisionner sur Scaleway selon le [runbook](../../deployment/SCALEWAY_DB_RUNTIME_ROLES.md) avant de configurer la délégation.
5. Corrélation par identifiant de requête et test de bout en bout connexion → catalogue → chat → run attribué depuis Conversations, avec reconnexion, expiration et révocation.
