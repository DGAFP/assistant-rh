# Livraison staging sur Kapsule

Le workflow `kapsule-staging.yml` valide le chart et construit les images sur runner GitHub sans secret pour les PR. Il publie ensuite dans `rg.fr-par.scw.cloud/assistant-rh-staging` et déploie des digests précis quand la livraison est activée. Aucun accès à la production ou aux anciens conteneurs Serverless.

## Activation proposée (non appliquée)

La livraison cible exclusivement `staging`, après promotion `dev` → `staging`.
Le job `required-checks` exige la réussite des workflows `CI Tests` et `CodeQL`
pour le même SHA et le même événement `push`. La publication reconstruit l’image,
la scanne avec Trivy, puis publie son digest. Une révision dépassée de `staging`
est refusée avant publication et avant déploiement.

L’environnement GitHub `kapsule-staging` est limité à la branche `staging` et
contient déjà `SCW_REGISTRY_SECRET_KEY`, une identité dédiée au registre du projet
`assistant-rh` (`c1d3f629-d08d-439d-ba76-f115ea06e579`). Les secrets applicatifs et
`scw-registry` existent dans Kubernetes. Ne jamais passer leurs valeurs dans les
valeurs Helm, les journaux ou les artefacts.

`KAPSULE_STAGING_CD_ENABLED` reste `false` jusqu’à l’approbation et la validation
des runners. Le mode retenu est ensuite automatique, sans approbation GitHub par
livraison. Le chemin historique reste actif et inchangé : déploiement Streamlit,
migrations/seed, ingestion, recette privée et préparation de release Release Please
continuent de s’exécuter sur `staging`. La nouvelle CD vise une base distincte et
s’ajoute à ce chemin sans le remplacer ; l’activer ou la désactiver n’a aucun effet
sur les déploiements historiques.

La livraison automatique utilise Flux : GitHub publie les images et un chart
OCI dans le registre existant ; `source-controller` récupère le chart et
`helm-controller` gère la release Helm existante. Il n’y a aucun runner privé ni
accès Kubernetes dans GitHub. Les contrôleurs proposés observent uniquement
`assistant-rh-api`, avec des Roles locaux. Le contrôleur Helm utilise le compte
`github-deploy` déjà limité à ce namespace. Aucun droit IAM Scaleway, GitHub App
ou permission d’organisation supplémentaire n’est nécessaire.

Le script `package-staging.py` utilise Helm pour figer les valeurs et le digest
scanné dans un chart de version `0.<run_number>.<run_attempt>` ; `appVersion`
contient le SHA source. Un hook de test en lecture seule vérifie `/healthz`, la
base et la configuration après le rollout. Le chart OCI n’est publié qu’après
les images et la vérification du SHA courant de `staging`.

Les manifests Flux proposés sont dans
`assistant-rh-infra/deliveries/staging-cicd-activation-2026-10-14/flux` et restent
non appliqués. Les objets `OCIRepository` et `HelmRelease` sont fournis suspendus.
L’adoption conserve le nom, le namespace et l’historique Helm. Activer seulement
après vérification de l’artefact, du rendu et des droits sur le cluster attendu.
Après une adoption réussie, les échecs d’upgrade ou de test peuvent être remédiés
par rollback Helm. Pour la première adoption, conserver la révision Helm précédente
et prévoir un rollback opérateur : Flux ne dispose pas encore de son historique
de remédiation. Aucune migration n’est exécutée. Une évolution de schéma nécessite un lot distinct.

Un workflow GitHub vert prouve la publication. Le déploiement est confirmé par
`HelmRelease Ready=True`, la révision OCI attendue et la réussite du test Helm.
Le mode automatique ne réactive pas les comptes de recette temporaires.
Pour arrêter une livraison, désactiver la variable CD **et** suspendre la
HelmRelease ; arrêter la publication seule n’annule pas un artefact déjà publié.
Pour un rollback opérateur, suspendre Flux avant `helm rollback` et préparer une
nouvelle livraison corrective avant reprise.

Le bouton `workflow_dispatch` dépend de la présence du workflow sur la branche
par défaut. La première activation repose sur la promotion `dev` → `staging` et
son événement `push`, sans dépendre de ce bouton.

## Configuration applicative

La livraison automatique et le déclenchement manuel acceptent uniquement `staging`. La promotion suit `dev` → `staging`.

Namespace `assistant-rh-api`, release/service `assistant-rh-api`, URL interne `http://assistant-rh-api.assistant-rh-api.svc.cluster.local:8000`. Aucun Ingress. L'isolation réseau est gérée par Terraform et autorise Conversations.

Le secret `scaleway-database-ca` doit contenir `postgres.pem` (CA de la nouvelle base Scaleway). Le chart le monte en lecture seule ; `PGSSLMODE=verify-ca` et `PGSSLROOTCERT=/etc/scaleway-ca/postgres.pem` imposent la validation. Le DSN ne doit pas les désactiver.

Le secret `assistant-rh-api-runtime` doit contenir `SCW_POSTGRES_DSN` pour `10.10.1.2:5432/assistant_rh` avec le compte dédié, `ALBERT_API_KEY` et les paramètres d'identité/délégation requis par la version livrée ; `SCALEWAY_API_KEY` active le fallback. Ajouter au besoin les bases URL documentées par l'API. Le DSN historique de `scaleway-staging` cible une autre base et ne doit pas être repris automatiquement.

Le nouveau staging a été initialisé le 14/10/2026 : schéma complet, migrations API, corpus et paramètres RAG, sans groupes, sessions, runs ni feedbacks historiques. Le compte API est `readonly` avec le rôle `arh_api`, sans CREATE ni TRUNCATE. Le marqueur `assistant-rh-schema-ready` a été créé après contrôle SQL et `/healthz` contre la vraie base. Pour une reconstruction, reprendre le schéma/corpus et appliquer les migrations avec le compte migrateur dans une opération distincte ; le pipeline n'utilise jamais les fixtures synthétiques locales pour ce bootstrap. Après vérification réelle du schéma et des grants runtime, créer le ConfigMap marqueur `assistant-rh-schema-ready` dans ce namespace. Sans ce marqueur, le rollout refuse de démarrer. Le compte API ne doit pas servir à migrer. `/healthz` vérifie aussi la configuration ; la liveness TCP évite un cycle de redémarrages en cas de panne de base.


## Vérification

Sur chaque PR : rendu et lint Helm, construction Docker sans publication. Avant publication, Trivy bloque toute vulnérabilité HIGH/CRITICAL de l’image ; le digest publié correspond à cette image scannée. Avant activation : PostgreSQL privé joignable avec certificat validé, schéma initialisé, comptes/grants appropriés, secrets présents et contrôleurs Flux prêts. Un `/healthz` ou heartbeat ne prouve pas une vraie conversation : tester ensuite authentification, `/v1/models`, échange SSE et droits individuels depuis Conversations.

La première livraison manuelle et la recette HTTP authentifiée ont réussi le
14/10/2026 (échange SSE, persistance, séparation de deux utilisateurs, logout).
Les comptes temporaires ont été désactivés. Cette preuve ne valide pas encore
Flux ni le prochain SHA : la première livraison automatique doit vérifier à
nouveau les rollouts et la santé. Une recette authentifiée future requiert des
comptes autorisés séparément.
