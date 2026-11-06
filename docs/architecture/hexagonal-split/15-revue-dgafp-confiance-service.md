# Note de revue DGAFP : déclinaison technique de la délégation

> Note du 2026-10-07, réalignée le 2026-10-12 sur l’extrait validé du DAT communiqué dans [#596](https://github.com/DGAFP/assistant-rh/issues/596). Détail : [contrat de délégation](14-conversations-delegation-contract.md).

## Principes confirmés

L’extrait confirme ProConnect/OIDC dans Conversations, l’admission sur invitation ou validation centrale DGAFP et la chaîne utilisateur → groupes → modèles ministériels. Conversations reste la référence des utilisateurs et habilitations ; son backend délègue identité individuelle et périmètre à l’API, qui vérifie le service et les informations déléguées sans dupliquer les appartenances.

Le contrôle d’accès est **auteur individuel + ministère actuellement autorisé**. Les groupes habilitent aux corpus ; ils ne partagent ni conversations, ni runs, ni feedbacks. Un ministère est fixé à la création d’une conversation. Un retrait bloque la prochaine question, l’historique concerné et tout nouvel accès documentaire ; l’historique est masqué puis rétabli si le droit revient. Les changements de droits portent auteur, date et objet dans l’audit d’administration.

Ces responsabilités ne sont plus un arbitrage global ouvert. L’ancien texte demandant de choisir « mTLS ou OAuth » est remplacé par le profil déjà implémenté.

## Profil livré, à rattacher au DAT dans la revue technique

| Élément | Mise en œuvre API livrée | Vérification complémentaire |
|---|---|---|
| Authentification du service et preuve déléguée | JWS compact EdDSA/Ed25519, `kid` épinglé ; identité `sub` UUID, `iss` et `aud` contrôlés | Clé privée exclusivement backend ; clés et audiences séparées par environnement |
| Durée et rejeu | ≤ 120 s, expiration stricte ; avance `iat`/`nbf` ≤ 30 s ; `jti` unique par tentative, anti-rejeu PostgreSQL partagé | Retry avec nouvelle assertion ; refus sur un autre réplica et après expiration |
| Rotation/révocation | Chevauchement ancien/nouveau `kid`, puis retrait et redéploiement API | Responsable, procédure et stockage des clés à renseigner avant recette |
| Opérations en cours | Retrait effectif à la requête suivante ; stream déjà autorisé terminé | Démonstration cible ; préciser les téléchargements avec #594 ; les octets déjà livrés ne sont pas révocables |
| Audit | `audit_session` HMAC-SHA-256 d’un identifiant interne aléatoire de session, clé dédiée | Pseudonyme renouvelé à la reconnexion, distinct des cookies, bearers et UUID utilisateur |
| Réseau | API uniquement joignable depuis le backend sur réseau privé | Contrôle de la topologie cible et absence d’exposition navigateur |

L’extrait du DAT ne nomme pas Ed25519 et ne fixe pas ces durées. Décrire ces paramètres comme une implémentation livrée, consigner leur rattachement au DAT et les éventuelles validations complémentaires du circuit de revue. Ne pas affirmer que l’extrait les approuve explicitement.

## Risques et garanties

Un backend Conversations compromis peut agir au nom des utilisateurs : limite explicite du mécanisme, réduite par le réseau privé, les clés dédiées, leur rotation et leur révocation. Une preuve utilisateur de bout en bout pourrait compléter le mécanisme ultérieurement ; elle n’est pas un prérequis réintroduit par cette note.

Aucun corpus sans habilitation ; aucune identité issue d’un email, SIRET ou champ libre du navigateur ; aucun jeton ProConnect, secret ou contenu de délégation dans les logs. L’identifiant de requête corrèle les journaux sans porter l’autorisation. Une bascule Albert/Scaleway interne à l’API conserve exactement le périmètre documentaire de la conversation.

Les pouvoirs administrateurs restent explicites et audités. B4 conserve son fonctionnement pendant la transition : pas de backfill des runs collectifs, aucune session de groupe ouvrant les ressources individuelles, aucune décision implicite de retrait de Streamlit.

## Recette encore nécessaire

La stack API #597 → #601 → #602 → #603 fusionnée dans `dev` ne vaut pas activation. Provisionner et vérifier les comptes runtime non propriétaires selon le [runbook #599](../../deployment/SCALEWAY_DB_RUNTIME_ROLES.md), puis consigner révision du fork, révision API, environnement et résultats.

Tester réellement depuis `DGAFP/conversations` : connexion ProConnect, approbation, catalogue multi-groupes, chat JSON/SSE, auteur du run, reconnexion/expiration/logout/désactivation, refus croisés entre utilisateurs, retrait/rétablissement des droits et feedback. Raccorder les documents protégés avec #594. Les tests locaux et mocks préparent cette recette ; ils ne clôturent pas #596.
