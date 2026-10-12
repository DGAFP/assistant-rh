# Note de revue DGAFP : délégation par confiance de service

> Note du 2026-10-07 soumise à la revue DGAFP pour [#596](https://github.com/DGAFP/assistant-rh/issues/596). Le DAT Assistant RH v0.4 (1er octobre 2026) classe le mécanisme de délégation Conversations → API parmi les arbitrages à clôturer. Détail technique : [contrat de délégation](14-conversations-delegation-contract.md).

## Décision demandée

Valider, pour la première version de l'accès individuel via Conversations, que **l'API Assistant RH fait confiance au backend Conversations authentifié** pour lui transmettre l'identité de l'utilisateur et ses habilitations, sans preuve d'identité de l'utilisateur de bout en bout. À défaut, consigner l'écart au DAT et retenir l'une des alternatives ci-dessous.

## Proposition

- Conversations reste la référence des utilisateurs, des groupes et des habilitations ministérielles ; l'accès est soumis à invitation ou validation par un administrateur central DGAFP.
- À chaque appel, le backend Conversations signe une assertion courte (≤ 120 s) portant l'identifiant stable de l'utilisateur, les modèles accordés et le ministère de la conversation. L'API vérifie la signature avec une clé publique épinglée et refuse tout appel non signé, même s'il fournit un identifiant.
- L'API ne conserve ni registre d'utilisateurs ni appartenance aux groupes. Elle borne chaque requête au périmètre délégué et rattache chaque run à son auteur, de façon immuable.
- L'API n'est joignable que depuis le backend Conversations, par réseau privé.

## Alternatives écartées

| Alternative | Motif |
|---|---|
| mTLS entre Conversations et l'API | Authentifie le service mais pas le contenu de la délégation (en-têtes non signés) ; impose une PKI et une terminaison TLS au niveau de l'API. |
| Client credentials via un serveur d'autorisation | Serveur d'autorisation à héberger et exploiter (ProConnect ne le fournit pas) ; la délégation reste à transporter à côté du jeton. |
| Preuve utilisateur de bout en bout (échange de jeton ProConnect) | Plus forte, mais dépend d'un mécanisme d'échange non disponible à ce jour ; reportée sans changement du modèle d'auteur. |

## Risques acceptés et mesures

| Risque | Mesures |
|---|---|
| Un backend Conversations compromis peut agir au nom de n'importe quel utilisateur | Réseau privé ; clé de signature dédiée, conservée côté serveur, renouvelée périodiquement ; révocation par retrait de la clé et redéploiement ; assertions de 120 s au plus. |
| Rejeu d'une assertion interceptée | Assertions courtes ; identifiant unique (`jti`) mémorisé par l'API et refusé s'il est déjà vu. |
| Habilitation retirée pendant une réponse | Le retrait s'applique dès la requête suivante ; une réponse en cours se termine ; les octets déjà téléchargés ne sont pas révocables. |

## Garanties maintenues

- Deux utilisateurs d'un même groupe ont des identifiants auteur distincts ; l'appartenance à un groupe ne donne accès ni aux runs ni aux feedbacks d'un autre utilisateur.
- Un utilisateur sans habilitation n'accède à aucun corpus ; un nom de modèle ne vaut pas autorisation.
- Aucun jeton ProConnect, secret de service ou contenu de délégation n'est journalisé ni transmis au navigateur.
- Le parcours historique par mot de passe de groupe (B4) reste inchangé pendant la transition ; aucun run collectif n'est réattribué à une personne.
