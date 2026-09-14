# Conversations : identité individuelle et habilitations aux corpus

> Note de conception du 2026-09-09 — direction retenue pour le temps 2, non implémentée.
> Aucun changement du contrat B4, aucune migration ni livraison runtime dans cette note.

## Périmètre et responsabilités

La cible est Conversations de La Suite numérique auto-hébergé. ProConnect peut être utilisé dès le départ ; un fournisseur OIDC auto-hébergé proposant identifiant/mot de passe est également possible. ProConnect n'est pas obligatoire. L'API Assistant RH reste indépendante du fournisseur d'identité. Cette note traite des **habilitations d'accès des agents aux corpus ministériels**, pas d'homologation de sécurité.

Conversations gère la connexion et l'identité individuelle. Assistant RH est l'autorité des droits métier : utilisateurs, affectations aux groupes et politiques ministérielles existantes. L'administration Streamlit permet à un administrateur autorisé d'affecter manuellement les nouveaux utilisateurs aux groupes. L'identité OIDC ne confère aucun droit métier : ni le mail, ni le SIRET, ni un groupe choisi librement par le client ne peuvent accorder un corpus.

Les frontières hexagonales existantes restent applicables : validation de la preuve au niveau des adaptateurs d'authentification, cas d'usage et politiques dans le core, persistance derrière les ports. Aucun microservice « bridge » distinct n'est requis par principe. Le protocole de confiance reste à préciser avant implémentation.

## Modèle conceptuel

Les noms suivants décrivent des concepts, pas un schéma SQL adopté.

| Concept | Rôle et invariants |
|---|---|
| Utilisateur Assistant RH | Identifiant interne durable, identité externe unique `(issuer, sub)`, statut en attente/actif/désactivé. L'issuer provient d'une preuve vérifiée ; aucune fusion automatique par mail. Un changement de fournisseur demande une procédure explicite de rattachement. |
| Appartenance utilisateur–groupe | Relation administrée, permettant plusieurs groupes sans décider ici de leur composition. Affectations et retraits traçables. |
| Groupe et politique ministère | Réutilisation des politiques existantes : ministères autorisés et défaut valide. Aucun droit implicite pour un utilisateur sans affectation. |
| Session individuelle Assistant RH | Référence à l'utilisateur, échéance courte, état révocable ; secret conservé uniquement côté serveur Conversations. Aucun token permanent sur le compte ni clé partagée de groupe. |
| Historique d'administration | Acteur administrateur, utilisateur ciblé, changement et date ; pas de bearer ni de preuve d'identité dans les journaux. |

À la première preuve valide, Assistant RH retrouve ou crée l'utilisateur de façon idempotente, y compris lors de connexions concurrentes. Un nouvel utilisateur est **en attente, sans accès par défaut**. L'écran Conversations indique cette attente ; l'administrateur affecte les groupes côté Streamlit. Le passage à actif et les conditions exactes d'émission pour un compte en attente restent à spécifier ; aucune session éventuellement créée pour ce parcours ne doit donner accès aux corpus.

## Flux cible et preuve d'identité

1. L'agent s'authentifie auprès du fournisseur OIDC via Conversations.
2. Le backend Conversations obtient une preuve vérifiable pour Assistant RH dans le cadre d'un échange ou d'une délégation serveur de confiance.
3. Assistant RH vérifie cette preuve, retrouve/crée l'utilisateur et évalue son statut et ses habilitations.
4. Pour un utilisateur habilité, Assistant RH émet une session individuelle courte. Conversations conserve son secret côté serveur, isolé par utilisateur/session de connexion, et l'utilise pour les appels API.
5. Le backend charge `GET /v1/models` avec cette session, puis transmet la même identité de session lors des chats et accès documentaires.

Un couple `issuer/sub` déclaratif, même envoyé depuis le backend, ne suffit pas. Un ID token ou access token ProConnect destiné à Conversations n'est pas automatiquement utilisable pour notre API. Le futur mécanisme devra fournir une preuve signée courte destinée à Assistant RH, vérifier signature, émetteur approuvé, audience, expiration et protection contre le rejeu, et lier sans ambiguïté la preuve à l'identité OIDC authentifiée. Si le signataire est un serveur délégant, distinguer son identité d'émetteur de l'issuer OIDC de l'utilisateur.

**À finaliser** : protocole d'échange/délégation, serveur autorisé à signer, clés et rotation, liaison au login, mécanisme anti-rejeu et fenêtres temporelles. Le flux général est retenu ; aucun protocole cryptographique particulier, endpoint nouveau ou TTL individuel exact n'est décidé ici.

## Sessions, expiration et révocation

À chaque reconnexion, une preuve valide permet une nouvelle émission ; un identifiant de compte mémorisé ne permet jamais de récupérer un ancien secret ou de créer une session. Le bearer ne passe pas dans le navigateur, les URL ou les logs.

La session Assistant RH peut expirer alors que Conversations est encore ouvert. Une nouvelle émission ou un renouvellement exige une preuve d'identité encore valide **et une réévaluation des habilitations**. Si Conversations ne peut plus apporter cette preuve, il impose une reconnexion. La seule présence du cookie Conversations ou d'un bearer RH expiré ne suffit pas. Le mécanisme retenu devra éviter les renouvellements concurrents incohérents et définir logout, révocation et propagation de fin de session.

Un retrait de groupe ou une désactivation s'applique **dès la prochaine requête**, sans attendre l'échéance du bearer : résolution des droits actuels ou contrôle de révision avec invalidation cohérente à chaque requête. Une photographie des droits dans un token valable jusqu'à expiration ne satisfait pas cette exigence. Le sort d'un stream déjà commencé doit être précisé séparément ; il ne doit pas permettre de nouvelles requêtes après retrait.

L'exigence couvre aussi l'émission **et la rédemption** des accès documentaires. Les capabilities et redirections S3 actuelles du [contrat B4/A3](02-api-contract.md) ne suffisent pas à promettre une révocation immédiate : une URL S3 déjà délivrée peut rester utilisable jusqu'à son expiration. Avant le temps 2, choisir un chemin contrôlé à chaque accès (par exemple lecture via backend/API avec vérification des droits), ou un mécanisme équivalent sans accès résiduel par lien préémis. Il s'agit d'une évolution à concevoir, pas d'une propriété livrée ; des bytes déjà téléchargés ne sont pas révocables.

## Catalogue, chat et documents

Le backend Conversations doit récupérer `GET /v1/models` avec la session individuelle et n'afficher que les modèles autorisés. Son catalogue global actuel doit être adapté ; une clé provider configurée au niveau du serveur ne porte pas automatiquement l'identité de l'agent. Aucun cache partagé ne doit mélanger les catalogues utilisateurs ; conserver le `no-store` du contrat et actualiser après changement de droits.

L'API contrôle les mêmes habilitations à chaque chat et accès document : choisir ou forger un identifiant de modèle n'accorde aucun droit. Le routage ministère reste celui de D2 ; un ministère inconnu ou interdit est refusé. Les documents doivent toujours appartenir aux sources finales persistées du run, avec en plus la politique d'accès historique à trancher ci-dessous. L'interface ne constitue jamais la barrière d'autorisation.

## Multi-groupes et historique : arbitrages nécessaires

| Question ouverte | Impact à analyser avant implémentation |
|---|---|
| Composition des groupes | Union, intersection ou contexte de groupe explicite validé par le serveur ne donnent pas les mêmes droits. Aucune option n'est adoptée ici ; définir conflits, groupes invalides et refus sans affectation. |
| Ministère par défaut | Plusieurs groupes peuvent avoir des défauts différents. Ne pas prendre silencieusement le premier ou un défaut global. Définir la résolution de l'alias `assistant-rh` et du modèle omis ; tout défaut doit rester autorisé. |
| Ownership des runs, feedbacks et documents | Le contrat actuel rattache les accès au groupe. Décider si le futur historique est individuel, partagé par groupe ou hybride, et comment vérifier auteur, groupe de création et droits courants. Aucun changement implicite d'ownership. |
| Historique B4 et mobilité | Définir l'accès aux anciens runs après rattachement individuel, retrait/changement de groupe ou désactivation. Ne pas réattribuer automatiquement les runs collectifs à une personne. Préserver les références de sources et l'audit existants. |

## Coexistence avec B4 et séquencement

B4 conserve son contrat actuel : **mot de passe de groupe → session opaque de huit heures, non renouvelable**, bornée à ce groupe. D6, les routes d'auth actuelles et les contrôles d'ownership du [contrat API](02-api-contract.md) restent inchangés. La session individuelle courte constitue un futur mécanisme distinct ; aucune durée ou capacité de renouvellement ne se déduit de B4.

1. Finaliser les arbitrages de confiance, multi-groupes, défaut, historique et documents ; compléter le contrat et les scénarios de validation dans une évolution dédiée.
2. Développer côté Assistant RH le modèle utilisateur, l'administration Streamlit, l'émission individuelle et les contrôles de droits ; côté Conversations, la délégation, la conservation serveur, la gestion d'expiration et le catalogue individuel.
3. Valider la coexistence de principaux groupe B4 et utilisateur individuel sans confusion de type ni conversion automatique ; conserver le parcours B4 pendant la transition explicitement planifiée.
4. Autoriser séparément la bascule et le retrait éventuel de B4 après validation. Cette note ne change ni les jalons du temps 1 ni leur statut au [LEDGER](LEDGER.md).

## Évidence Conversations vérifiée

Lecture du dépôt public à la révision stable [`4f73043f731875fdc3f12bca9f15864c3da0cd90`](https://github.com/suitenumerique/conversations/tree/4f73043f731875fdc3f12bca9f15864c3da0cd90), le 2026-09-09 :

- [`core/models.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/core/models.py) : utilisateur OIDC, `sub`, `organization_siret`, `PermissionsMixin` Django. Cela ne constitue pas une gestion des habilitations RH prête à l'emploi ; les comptes administrateurs Django ne prouvent pas l'existence d'un login utilisateur local prêt.
- [`core/authentication/backends.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/core/authentication/backends.py) : stockage du SIRET et filtre éventuel d'accès à l'application par rôles, distincts des droits sur les corpus.
- [`conversations/oidc_settings.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/conversations/oidc_settings.py) : fournisseur et endpoints OIDC configurables.
- [`chat/views/llm_config.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/chat/views/llm_config.py) : catalogue issu des configurations globales, explicitement non filtré par utilisateur.
- [`chat/llm_configuration.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/chat/llm_configuration.py) : clé provider configurée côté serveur. Ces fichiers n'établissent aucune délégation automatique d'identité vers Assistant RH.

## Critères d'acceptation du futur chantier

- Deux connexions concurrentes de la même identité vérifiée retrouvent un seul utilisateur ; deux issuers avec le même `sub` restent distincts. Mail/SIRET identiques n'accordent ni fusion ni habilitation.
- Un nouveau compte reste en attente sans corpus ; seule une affectation administrative autorisée ouvre des droits, avec audit. Un groupe fourni par le client ne peut pas accorder un droit.
- Une preuve expirée, de mauvaise audience, d'émetteur non approuvé, rejouée ou déclarative est refusée ; les deux fournisseurs envisagés passent par la même frontière d'auth API.
- Le secret RH reste côté serveur et isolé entre utilisateurs. Reconnexion et expiration en cours de session Conversations exigent une preuve valide et de nouveaux contrôles ; sinon reconnexion visible.
- Retrait/désactivation bloque dès la requête suivante le catalogue concerné, le chat et tout nouvel accès documentaire, y compris via un lien déjà émis selon le mécanisme à retenir.
- Un modèle forgé ou un catalogue obsolète ne contourne pas l'API. Les cas multi-groupes, défaut ambigu et historique après mobilité ont des résultats spécifiés avant codage.
- Les régressions B4 couvrent ses huit heures non renouvelables, son login groupe et son ownership actuel pendant toute la coexistence. Aucun test runtime n'est ajouté par cette note documentaire.
