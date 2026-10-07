# Conversations : identité individuelle et habilitations aux corpus

> Note de conception du 2026-09-09, réalignée le 2026-10-07 sur le DAT Assistant RH v0.4 (1er octobre 2026) et sur les choix de [#596](https://github.com/DGAFP/assistant-rh/issues/596) du 6 octobre 2026. Direction retenue pour le temps 2, non implémentée.
> Aucun changement du contrat B4, aucune migration ni livraison runtime dans cette note. En cas d'écart, le DAT prévaut ou l'écart est soumis à revue.

## Périmètre et responsabilités

La cible est une instance dédiée de Conversations de La Suite numérique. ProConnect assure l'authentification des agents dans Conversations via OpenID Connect. Cette note traite des **habilitations d'accès des agents aux corpus ministériels**, pas d'homologation de sécurité.

Conversations est la référence des utilisateurs, des groupes et des habilitations ministérielles. L'accès est soumis à une invitation ou à une validation par un administrateur central DGAFP ; une authentification réussie ne confère aucun droit sur les corpus. Le service d'administration modifie les habilitations par une interface applicative du backend Conversations, jamais directement dans ses tables.

L'API Assistant RH ne duplique pas les appartenances aux groupes et ne tient pas de registre d'utilisateurs. Elle authentifie le service appelant, vérifie le périmètre délégué par le backend Conversations et applique le filtrage documentaire correspondant. Ni le mail, ni le SIRET, ni un groupe ou un modèle choisi par le client ne peuvent accorder un corpus.

Les frontières hexagonales existantes restent applicables : validation de la délégation au niveau des adaptateurs d'authentification, cas d'usage et politiques dans le core, persistance derrière les ports.

## Choix retenus (#596, 6 octobre 2026)

| Sujet | Choix |
|---|---|
| Délégation | **Confiance de service.** L'API n'accepte que les appels du backend Conversations, joignable uniquement par le réseau privé et authentifié comme service (mTLS ou client credentials OAuth, à choisir). Le backend transmet l'identifiant de l'utilisateur et le périmètre autorisé. Pas de preuve utilisateur de bout en bout dans cette première version. Le DAT classe ce mécanisme parmi les arbitrages à clôturer : ce choix est proposé à la revue DGAFP. |
| Identifiant auteur | Identifiant stable de l'utilisateur Conversations, persisté comme auteur des runs. Pas de registre d'utilisateurs côté API. |
| Accès à un run individuel | Auteur + habilitation courante sur le ministère du run, quel que soit le groupe courant. |
| Restrictions SQL | Écritures directes sur les feedbacks individuels : [#599](https://github.com/DGAFP/assistant-rh/issues/599). |

## Contrat de délégation

Le contrat entre le backend Conversations et l'API précise :

- les restrictions réseau : seul le backend Conversations peut joindre l'API ;
- l'authentification du service appelant, en plus du réseau, avec rotation et révocation de ses secrets ;
- le contenu transmis à chaque requête : identifiant utilisateur stable, ministère de la conversation et modèles autorisés ;
- le délai de prise en compte d'un retrait d'habilitation ;
- le comportement des streams et des téléchargements déjà en cours lors d'un retrait.

Un appel qui ne provient pas du backend authentifié est refusé, même s'il fournit un identifiant utilisateur. La stabilité de l'identifiant Conversations doit être vérifiée (jamais réattribué, inchangé à la reconnexion), ainsi que le cas d'un compte supprimé puis recréé.

**Risque accepté** : un backend Conversations compromis peut agir au nom de n'importe quel utilisateur. Il est réduit par les restrictions réseau, l'authentification de service et la rotation des secrets. Une preuve utilisateur de bout en bout pourra être ajoutée plus tard sans changer le modèle d'auteur.

## Flux cible

1. L'agent s'authentifie auprès de ProConnect via Conversations.
2. Le backend Conversations vérifie que l'utilisateur est admis et lit ses habilitations courantes.
3. À la création d'une conversation, l'utilisateur choisit un ministère parmi ses habilitations ; ce périmètre reste fixe pour toute la conversation.
4. Le backend filtre le catalogue des modèles selon les habilitations courantes, puis appelle l'API en transmettant l'identifiant utilisateur et le périmètre autorisé.
5. L'API authentifie le service appelant, vérifie que le modèle demandé correspond au périmètre délégué et applique le filtrage documentaire. Le nom d'un modèle ne vaut pas autorisation.

## Habilitations, retrait et historique

Le backend Conversations vérifie les habilitations courantes à chaque nouvelle question, consultation de l'historique et accès documentaire. L'API vérifie à chaque requête le périmètre délégué. Un retrait d'habilitation ou une désactivation s'applique **dès la requête suivante**.

Le retrait d'une habilitation bloque les nouvelles questions, les nouveaux accès documentaires et la consultation des anciennes conversations du ministère concerné. Ces conversations et leurs runs sont masqués sans être supprimés, et redeviennent accessibles si l'habilitation est rétablie. Les modifications de droits sont journalisées avec leur auteur, leur date et leur objet.

Un utilisateur peut appartenir à plusieurs groupes. L'accès à ses runs, feedbacks et sources repose sur son identité d'auteur et sur l'habilitation courante au ministère du run, et non sur le groupe : un changement de groupe qui conserve ce ministère conserve l'accès. L'appartenance à un même groupe ne donne aucun accès aux runs ou feedbacks d'un autre utilisateur. La propriété d'un run ne confère pas un droit perpétuel au corpus.

## Documents

Les documents sont délivrés via le backend, après vérification des habilitations et du périmètre du document à chaque accès. Le bucket reste privé et aucun lien S3 direct n'est transmis au navigateur. Les capabilities et redirections S3 du [contrat B4/A3](02-api-contract.md) ne s'appliquent pas à cette cible. Le comportement d'un téléchargement en cours lors d'un retrait est défini par le contrat de délégation ; des octets déjà téléchargés ne sont pas révocables.

## Journaux

Un identifiant de requête corrèle Conversations et l'API. Les jetons ProConnect, les secrets de service et le contenu de la délégation ne sont jamais journalisés. Le pseudonyme d'audit des feedbacks reste à fournir par #596.

## Coexistence avec B4 et séquencement

B4 conserve son contrat actuel : **mot de passe de groupe → session opaque de huit heures, non renouvelable**, bornée à ce groupe. D6, les routes d'auth actuelles et les contrôles d'ownership collectifs du [contrat API](02-api-contract.md) restent inchangés. Principaux groupe et utilisateur restent distincts : aucune réattribution automatique des anciens runs collectifs, aucune session de groupe permettant d'accéder aux runs individuels.

1. Valider en revue DGAFP le choix de délégation par confiance de service, ou consigner l'écart au DAT.
2. Développer côté Conversations l'admission, les habilitations ministérielles, le filtrage du catalogue et la délégation ; côté Assistant RH l'authentification du service, la vérification du périmètre délégué et l'auteur des runs.
3. Valider la coexistence des principaux groupe B4 et utilisateur individuel sans confusion de type ni conversion automatique.
4. Autoriser séparément la bascule et le retrait éventuel de B4 après validation. Cette note ne change ni les jalons du temps 1 ni leur statut au [LEDGER](LEDGER.md).

## Évidence Conversations vérifiée

Lecture du dépôt public à la révision stable [`4f73043f731875fdc3f12bca9f15864c3da0cd90`](https://github.com/suitenumerique/conversations/tree/4f73043f731875fdc3f12bca9f15864c3da0cd90), le 2026-09-09 :

- [`core/models.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/core/models.py) : utilisateur OIDC, `sub`, `organization_siret`, `PermissionsMixin` Django. Cela ne constitue pas une gestion des habilitations RH prête à l'emploi ; les comptes administrateurs Django ne prouvent pas l'existence d'un login utilisateur local prêt.
- [`core/authentication/backends.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/core/authentication/backends.py) : stockage du SIRET et filtre éventuel d'accès à l'application par rôles, distincts des droits sur les corpus.
- [`conversations/oidc_settings.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/conversations/oidc_settings.py) : fournisseur et endpoints OIDC configurables.
- [`chat/views/llm_config.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/chat/views/llm_config.py) : catalogue issu des configurations globales, explicitement non filtré par utilisateur.
- [`chat/llm_configuration.py`](https://github.com/suitenumerique/conversations/blob/4f73043f731875fdc3f12bca9f15864c3da0cd90/src/backend/chat/llm_configuration.py) : clé provider configurée côté serveur. Ces fichiers n'établissent aucune délégation automatique d'identité vers Assistant RH.

## Critères d'acceptation du futur chantier

Les critères de référence sont ceux de [#596](https://github.com/DGAFP/assistant-rh/issues/596). En résumé :

- Un appel qui ne provient pas du backend Conversations authentifié est refusé, depuis un autre réseau, sans authentification de service ou avec un secret révoqué.
- Deux utilisateurs d'un même groupe ont des identifiants auteur distincts ; aucun cache, session ou catalogue ne fuit entre utilisateurs.
- Un utilisateur sans habilitation n'accède à aucun corpus ; un modèle hors du périmètre délégué est refusé ; un retrait prend effet dès la requête suivante.
- L'API ne stocke ni appartenance aux groupes ni registre d'utilisateurs.
- Après retrait, les anciens runs du ministère sont masqués puis restaurés si le droit revient ; un changement de groupe qui conserve le ministère conserve l'accès à ses runs.
- Documents délivrés via le backend, sans lien S3 direct.
- Les régressions B4 couvrent ses huit heures non renouvelables, son login groupe et son ownership actuel pendant toute la coexistence. Aucun test runtime n'est ajouté par cette note documentaire.
