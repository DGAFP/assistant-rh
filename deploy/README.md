# Livraison staging sur Kapsule

Le workflow `kapsule-staging.yml` valide le chart et construit les images sur runner GitHub sans secret pour les PR. Il publie ensuite dans `rg.fr-par.scw.cloud/assistant-rh-staging` et déploie des digests précis quand la livraison est activée. Aucun accès à la production ou aux anciens conteneurs Serverless.

## Activation

L'environnement GitHub distinct `kapsule-staging` doit contenir `SCW_REGISTRY_SECRET_KEY`, clé d'une application IAM dédiée avec `ContainerRegistryFullAccess` sur le projet Assistant RH. Les droits IAM manquent actuellement pour créer cette identité. Ne pas copier la clé personnelle de l'opérateur. Provisionner aussi le secret Kubernetes `scw-registry` avec une identité de lecture du registre. Les secrets applicatifs sont préexistants dans Kubernetes ; aucun secret n'est passé aux valeurs Helm, aux logs ou aux artefacts GitHub.

La publication se fait sur runner GitHub hébergé ; le déploiement se fait sur un runner privé dédié portant les labels `self-hosted`, `linux`, `x64`, `api-staging-deploy`, accessible uniquement aux workflows de confiance du dépôt. **Ne pas exécuter les jobs de PR sur ce runner.** Installer `git`, `node` compatible avec checkout v5, `bash`, `kubectl` et Helm3.19.0. Son kubeconfig doit cibler exactement `https://fce3ddcb-b9ff-49fb-8b7d-aca605272b0a.api.k8s.fr-par.scw.cloud:6443`, avec une identité Kubernetes limitée au namespace cible, et la CA du cluster. La sortie réseau doit être celle de la passerelle staging51.15.255.192, déjà autorisée. Aucun runner n'est encore enregistré ; le RBAC proposé est dans `deploy/ci-rbac.yaml` et a été appliqué au seul cluster staging le13/10/2026 (droits vérifiés).

La variable de dépôt `KAPSULE_STAGING_CD_ENABLED` reste `false` tant que ces prérequis ne sont pas satisfaits. Après validation, un `workflow_dispatch` avec `deploy=true` effectue la première livraison ; activer ensuite la variable pour les livraisons automatiques. Le bouton manuel n'est disponible que lorsque le workflow est intégré à la branche par défaut du dépôt. Les branches autorisées sont précisées ci-dessous et protégées par l'environnement GitHub.

Le script refuse tout autre cluster, vérifie les objets prérequis, attend le rollout et effectue un contrôle de santé. `--atomic` restaure la release applicative en cas d'échec ; **cela ne restaure pas la base après une migration**. N'appliquer que des migrations compatibles avec la version précédente et conserver une sauvegarde avant toute migration destructive. Un rollback volontaire utilise `helm history` puis `helm rollback` dans le même namespace, après vérification de la compatibilité du schéma.

## Configuration applicative

La livraison automatique et le déclenchement manuel acceptent uniquement `staging`. La promotion suit `dev` → `staging`.

Namespace `assistant-rh-api`, release/service `assistant-rh-api`, URL interne `http://assistant-rh-api.assistant-rh-api.svc.cluster.local:8000`. Aucun Ingress. L'isolation réseau est gérée par Terraform et autorise Conversations.

Le secret `scaleway-database-ca` doit contenir `postgres.pem` (CA de la nouvelle base Scaleway). Le chart le monte en lecture seule ; `PGSSLMODE=verify-ca` et `PGSSLROOTCERT=/etc/scaleway-ca/postgres.pem` imposent la validation. Le DSN ne doit pas les désactiver.

Le secret `assistant-rh-api-runtime` doit contenir `SCW_POSTGRES_DSN` pour10.10.1.2:5432/assistant_rh avec le compte dédié, `ALBERT_API_KEY` et les paramètres d'identité/délégation requis par la version livrée ; `SCALEWAY_API_KEY` active le fallback. Ajouter au besoin les bases URL documentées par l'API. Le DSN historique de `scaleway-staging` cible une autre base et ne doit pas être repris automatiquement.

La base nouvelle est vide. Reprendre le schéma/corpus et appliquer les migrations avec le compte migrateur dans une opération distincte ; le pipeline n'utilise jamais les fixtures synthétiques locales pour ce bootstrap. Après vérification réelle du schéma et des grants runtime, créer le ConfigMap marqueur `assistant-rh-schema-ready` dans ce namespace. Sans ce marqueur, le rollout refuse de démarrer. Le compte API ne doit pas servir à migrer. `/healthz` vérifie aussi la configuration ; la liveness TCP évite un cycle de redémarrages en cas de panne de base.


## Vérification

Sur chaque PR : rendu et lint Helm, construction Docker sans publication. Avant publication, Trivy bloque toute vulnérabilité HIGH/CRITICAL de l’image ; le digest publié correspond à cette image scannée. Avant activation : PostgreSQL privé joignable avec certificat validé, schéma initialisé, comptes/grants appropriés, secrets présents, runner connecté et une livraison manuelle réussie. Un `/healthz` ou heartbeat ne prouve pas une vraie conversation : tester ensuite authentification, `/v1/models`, échange SSE et droits individuels depuis Conversations.

Les runners JIT sont préparés par `assistant-rh-infra/scripts/start-staging-runner.py`. Le mode `--probe` a validé les outils, la connectivité GitHub et l’isolation RBAC des deux namespaces. Un runner n’est enregistré que pour un job de déploiement staging en attente, puis expire après son travail. Le renouvellement entièrement automatique requiert encore une GitHub App dédiée et ARC ; ne pas activer la variable CD avant cela.
