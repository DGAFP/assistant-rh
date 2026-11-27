# #596 — Recette identité et habilitations en staging

## Bilan du 27 novembre 2026

**Recette identité/habilitations qualifiée dans le périmètre convenu.** Le retour de déconnexion ProConnect et deux longs flux SSE natifs Chromium sont validés au complément de campagne. Le secours Albert → Scaleway est exclu sur instruction explicite du porteur le 27 novembre : cette exclusion autorise la clôture de #596 dans ce périmètre et ne constitue pas une validation du secours. La phase E reste ouverte.

[Preuve agrégée et inventaire des contrôles](../../evals/evidence/identity_596_staging_20261127.json).

Révisions : API `2043a66b9da9b88a7e44c0ce1efdcc3896a36353`, image `sha256:5f0eff1de8629ca7e7c2f827062f000322968b995a35f3a3a8f6861bf160673b` ; backend Conversations `d38bec4eeaebc911b5278d404dc8eb5879c9d1ce`, image `sha256:374f1985a3f8425ab02a8ce8a79f58c53da37e2031cec63d92e5893333ec4663`. Les onze modules d’identité, délégation, documents et feedback contrôlés correspondent exactement à ce commit du fork. Le frontend annonce `0.0.28`.

## Méthode et limites

Le login local reste le défaut public. La route explicite `/api/v1.0/proconnect/authenticate/` mène à `fca.integ01.dev-agentconnect.fr`, avec PKCE et le callback HTTPS staging. Les identités choisies passent par le [FI officiel de test ProConnect](https://partenaires.proconnect.gouv.fr/docs/fournisseur-service/identifiants-fi-test), avec des sujets distincts, des adresses synthétiques et ACR `eidas1`. Aucun compte personnel ni MFA réelle n’est utilisé. Les invitations CSV/SMTP sont désactivées ; la voie d’admission testée est l’administration centrale Django.

Configuration observée : émetteur `urn:assistant-rh:conversations:staging`, audience `assistant-rh-api`, `kid` actif `staging-2026-10`. OIDC et PKCE sont activés ; le login local reste le défaut et les invitations sont désactivées. Ces identifiants de configuration ne sont pas des secrets.

Deux ensembles sont distingués :

- Les premiers essais live ont obtenu **trois réponses SSE sous sessions locales et deux sous sessions ProConnect**, ainsi que l’écriture et les refus croisés de feedback. La question générique a parfois produit uniquement des sources publiques. Le relais SQL de recette a aussi coupé des connexions inactives ; ce défaut du banc a été corrigé et ses fixtures réconciliées. Ces essais sont partiels, leur succès de génération ne vaut pas succès de la matrice entière.
- Les matrices déterministes utilisent des **runs et associations message/completion/source préparés explicitement**, avec des documents privés existants. Le HTTPS, l’administration, les sessions, les signatures, les contrôles API/SQL et la délivrance PDF sont réels ; **aucune inférence n’est appelée dans ces matrices**. Elles isolent l’autorisation de la variabilité du retrieval et de la latence IA. Elles ne remplacent ni la recette du widget de feedback ni celle des cartes de sources dans #533/#595.

Les scripts, checkpoints et journaux bruts sont archivés sur l’hôte opérateur hors Git dans `staging-recipe-20261127.tar.gz`, mode `0600`, avec répertoire `0700` et manifeste SHA-256. Seuls les agrégats sont versionnés. Les scripts sont des outils de campagne, pas une nouvelle surface produit.

## Résultats

| Campagne | Résultat | Ce qui est démontré |
| --- | --- | --- |
| HTTPS local, fixtures contrôlées | **170/170 contrôles** | Admission distincte de l’authentification ; administrateur central et refus d’écriture au staff non central ; union multi-groupes et catalogue vide ; ministère immuable ; absence d’accès aux conversations, feedbacks et PDF du pair ; audit ; retries sans écriture ; retrait immédiat, masquage/restauration par un autre groupe ; logout, reconnexion, désactivation/réadmission et expiration forcée du cache ; auteur neuf après recréation. |
| ProConnect officiel, fixtures contrôlées | **112/113 contrôles** | Premier compte non admis refusé ; aucun corpus déduit de l’email ou du SIRET ; UUID distincts/stables ; sessions OIDC et pseudonymes serveur ; groupes, isolation, documents et feedbacks ; retrait/rétablissement ; session révoquée après logout ; reconnexion après expiration et désactivation ; même sujet OIDC recréé avec nouvel UUID, sans admission ni accès aux anciens runs. |
| Complément navigateur ProConnect | **10/10 contrôles** | Retour complet du logout, ancien cookie refusé, UUID stable après reconnexion ; deux flux Chromium réels terminés en **51.922 s et 60.110 s**, protocole **h2**, réponses textuelles et deux runs `completed` attribués aux deux auteurs distincts. |
| Rotation isolée dans Kapsule | **8/8 contrôles** | Même image API dans un canary privé : ancien/nouveau `kid` acceptés, retrait de l’ancien après rollout, refus d’un JWT ancien fraîchement signé et non rejoué, nouvelle clé toujours valide, restauration puis suppression du canary. Les clés et le déploiement actifs ne sont pas modifiés. |
| Cache partagé entre réplicas | **2/2 contrôles** | Les sessions locales et OIDC des fixtures sont lues avec le même auteur et nonce d’audit depuis les deux backends. |
| Corrélation et journaux applicatifs | **32/32 identifiants corrélés** | Les identifiants backend observés sont retrouvés dans les logs API. Aucun JWS compact, DSN PostgreSQL ni URL de stockage signée n’est détecté dans les journaux applicatifs contrôlés. Aucune entrée applicative/callback n’est disponible dans les logs Traefik collectés : leur couverture ne permet pas de conclure sur une ligne d’accès qui n’a pas été observée. |
| Topologie | Conforme au périmètre lu | Service API `ClusterIP`, sans IP externe ni ingress public ; accès par le backend via le DNS privé. |

La durée de session configurée est **43 200 s (12 h)** ; l’expiration a été forcée sur les seules sessions de recette, sans attendre 12 h. La délégation reste Ed25519, durée maximale 120 s et nouveau `jti` par tentative. Mauvaise signature, audience/émetteur erronés, expiration, durée excessive, `kid` inconnu et rejeu sur le second réplica sont refusés. Les principes DAT sont ceux du 12 octobre ; ces paramètres sont le profil technique observé, sans prétendre qu’ils sont imposés mot pour mot par le DAT.

Les migrations, comptes runtime non propriétaires et la coexistence B4 ont leur qualification M2 dans [#532](https://github.com/DGAFP/assistant-rh/issues/532), [#599](https://github.com/DGAFP/assistant-rh/issues/599) et la [PR de preuves #656](https://github.com/DGAFP/assistant-rh/pull/656). Ils ne sont pas réattribués à des groupes par cette recette.

## Complément de recette et décision de périmètre

Le porteur demande explicitement le 27 novembre : « ignorons la partie scaleway, mais faisons le reste ». La bascule Albert → Scaleway est donc **exclue de cette recette et de la clôture de #596**. Le runtime reste sans clé Scaleway ; le constat historique de panne du canary est conservé dans la preuve. Le principe de conservation du périmètre en cas de changement de fournisseur reste applicable, mais aucune bascule ni continuité du secours n'est attestée ici.

Le test de logout attend maintenant le retour effectif sur le domaine staging au lieu de s'arrêter au premier document intermédiaire du FI. Les deux routes de confirmation FI/fédération, puis `/api/v1.0/logout-callback/?state=…`, conduisent à la page d'accueil HTTP 200. L'ancien cookie retourne 403, et la reconnexion retrouve le même UUID. **Le défaut observé était l'attente du banc de recette** ; aucun code ni paramètre de logout applicatif n'est modifié. Ce parcours suit la [déconnexion OIDC documentée par ProConnect](https://partenaires.proconnect.gouv.fr/docs/fournisseur-service/implementation_technique).

Le complément exécute deux générations avec `fetch` et `ReadableStream` **dans Chromium**, sous deux sessions ProConnect distinctes, via le point d'entrée HTTPS public. Il reprend exactement la question privée des essais HTTPX qui échouaient ; seul son SHA-256 est publié. Les deux flux se terminent avec `[DONE]`, sans trame d'erreur, en **51.922 s et 60.110 s** (h2). Le SQL confirme deux runs `completed` avec les deux auteurs attendus. L'admission et les groupes de ce seul diagnostic de transport sont préparés par l'opérateur ORM ; les opérations d'administration HTTPS et leur audit restent prouvés par les matrices initiales.

Les erreurs Python/OpenSSL historiques **ne sont pas reproduites dans Chromium**. Leur cause exacte reste non attribuée : ce résultat qualifie le transport navigateur du parcours d'identité, sans prétendre corriger tous les clients ni valider la fenêtre de stabilité de #536. Le rapport initial à 112/113 et ses erreurs restent conservés ; le complément apporte la preuve manquante de retour de logout. Une première tentative diagnostique a aussi été interrompue par une collision de noms de groupes du banc, puis nettoyée avant le rejeu corrigé ; elle n'a appelé aucune inférence.

## Nettoyage et reproduction

Les comptes synthétiques, groupes, sessions, associations, feedbacks, runs, traces et canaries sont supprimés. Un run annulé qui a finalisé après la fermeture du client a été identifié par la fenêtre, la question et les opérations d’administration de la campagne, puis supprimé. Le contrôle final retrouve **15 comptes applicatifs**, comme avant la campagne, et zéro compte/groupe/run/canary appartenant à la recette. Le complément a également supprimé ses deux runs réels et toutes ses fixtures après la fin des producteurs. Les deux archives privées et leurs manifestes sont conservés. Le relais opérateur loopback et son port-forward `55436` sont arrêtés ; le tunnel et les ACL préexistants de l’opérateur restent intacts.

Pour rejouer : utiliser l’archive privée et le manifeste, confirmer le cluster et le DSN TLS staging, préparer des identités à préfixe unique, exécuter les changements d’habilitation par l’administration HTTPS puis les contrôles et le nettoyage ciblés. Les matrices contrôlées ne nécessitent aucune requête IA. Pour un essai de génération, enregistrer les UUID de comptes et conversations avant l’appel, utiliser `stop-streaming` et attendre la fin du producteur avant de supprimer ses fixtures ; fermer le navigateur ne stoppe pas un tour dans cette version du fork. L’archive conserve les essais historiques et leurs correctifs de banc ; ils ne sont pas tous des campagnes validées.

La recette navigateur intégrée et la stabilité conservent leurs critères propres dans #533, #595 et #536. La recette permet de clôturer #596 dans le périmètre convenu, avec exclusion explicite du secours Scaleway. Elle ne modifie aucun code ou credential actif et ne clôture pas la phase E.
