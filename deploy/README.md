# Livraison de l’API Assistant RH

Les PR ciblent `main`. Une fusion ne publie pas d’image et ne déploie rien.
Un tag protégé `vX.Y.Z-rc.N` livre le staging ; `vX.Y.Z` promeut la RC validée
**de même version et sur le même commit**, sans reconstruction d’image.
Voir aussi [le fonctionnement Git](../docs/git_flow.md).

## Parcours de livraison

1. Intégrer la PR et ses versions/lockfiles, puis créer une RC sur un commit
   déjà intégré dans main. Tests synthétiques, CodeQL et validation Helm/Docker
   tournent sur le tag ; une CI de PR ne prouve pas son SHA source.
2. Le succès de `Kapsule staging` déclenche `Tagged API delivery`, dont la
   définition privilégiée vient toujours de main. La qualification privée M0b
   est rejouée sur le SHA exact avec le secret déjà dans `scaleway-staging`.
3. Le job construit et scanne l’image API (vulnérabilités HIGH/CRITICAL et secrets),
   fige son digest et les valeurs de la cible dans un chart OCI, puis publie au
   registre. Flux utilise les contrôleurs et le compte `github-deploy` existants.
   GitHub ne possède aucun accès Kubernetes.
4. Flux doit installer cette révision précise, terminer le rollout et réussir
   les tests Helm. Un opérateur réalise alors la recette authentifiée depuis
   Conversations : authentification, vraie réponse SSE et isolation des utilisateurs.
   Un `/healthz` seul ou une publication OCI ne suffit pas.
5. Après validation du staging, le stable copie et rescane le digest exact de la
   RC vers le registre production. Il exige sa propre preuve de rollout/recette.
   La production reste désactivée jusqu’à son provisionnement explicite.

## Consigner la preuve

Un opérateur configuré dans `RELEASE_VALIDATORS` prépare ce JSON après succès
des contrôles, avec l’URL du rapport dans l’issue ou la PR de recette :

```json
{
  "tag": "v0.15.0-rc.1",
  "source_sha": "<SHA exact de 40 caractères>",
  "target": "staging",
  "checks": {"authentication": true, "sse": true, "isolation": true},
  "report_url": "https://github.com/DGAFP/assistant-rh/issues/<rapport>"
}
```

```bash
export GH_TOKEN="$(gh auth token)"
export RELEASE_VALIDATORS=pcasad
python3 deploy/api/release.py confirm \
  --tag v0.15.0-rc.1 --recipe /prive/recipe.json \
  --kubeconfig /prive/kubeconfig
```

La commande lit le cluster sans le modifier. Elle vérifie son identité, les
observed generations Flux, le digest du chart, les tests Helm et l’image API
prête. Le reçu conserve les overrides Helm et les images Conversations utilisées.
Le token opérateur doit pouvoir écrire les assets de Release et statuts de
Deployment ; les secrets/kubeconfigs restent hors Git et hors des artefacts.

La Release conserve `reservation.json`, le chart, `delivery.json` et un
`receipt.json` immuable, accepté seulement si un validateur humain autorisé l’a
publié et s’il désigne le manifeste exact. Pour invalider une preuve, ajouter
`revocation.json` à la Release et préparer une correction ; ne pas modifier le
reçu ni déplacer le tag.

## Reprise et rollback

Une livraison garde le verrou GitHub jusqu’à sa recette. Un GitHub Deployment
persistant bloque aussi la suivante après échec, annulation ou expiration ;
le job attend la preuve pendant trente minutes. Examiner Flux, terminer la
recette puis lancer `Tagged API delivery` sur **main**, action `resume`, avec
le tag existant. Les archives déjà publiées sont réutilisées.

Pour revenir à une version validée, lancer ce workflow sur main, action
`redeploy`, avec son tag : même digest, nouveau chart de séquence supérieure,
aucune reconstruction ni modification de tag. Le run ID identifie cette reprise.
Sa preuve utilise `confirm --delivery-id <run-id>` ; renseigner aussi cet ID
pour reprendre le job. Les assets portent le préfixe `redelivery-<run-id>-`.
Une reprise validée clôt toute la chaîne des tentatives remplacées. Sans version
validée connue, traiter la première adoption avec un opérateur.

Les charts suivent `0.<séquence suivante du registre>.0`, au-dessus des versions
historiques, pour rester compatibles avec le sélecteur Flux existant. Une reprise
ne remplace jamais un chart de contenu différent. Désactiver
`TAGGED_RELEASES_ENABLED` arrête les nouveaux jobs, pas les charts déjà publiés.
Suspendre Flux avant un rollback Helm d’urgence, puis réconcilier le chart attendu
avant reprise. Les migrations restent des opérations distinctes et approuvées.

## Activation GitHub

- `TAGGED_RELEASES_ENABLED=true`, `RELEASE_VALIDATORS=pcasad`.
- `KAPSULE_STAGING_CD_ENABLED=false` et `LEGACY_STAGING_AUTOMATION_ENABLED=false`.
- Création des tags `v*` réservée aux administrateurs ; mises à jour et suppressions
  interdites par une règle séparée sans contournement. Conserver les règles main.
- `kapsule-staging` autorise **main seulement**, avec son secret existant
  `SCW_REGISTRY_SECRET_KEY`. Le workflow privilégié tourne sur main, pas sur le tag.
- Conserver `KAPSULE_PRODUCTION_CD_ENABLED=false`. Avant la première RC destinée
  à la production, revoir `deploy/api/production.yaml`, créer son registre et son
  cluster distincts, préparer Flux et `kapsule-production` limité à main, avec
  `SCW_REGISTRY_SECRET_KEY` pour la cible et `SCW_STAGING_REGISTRY_READ_KEY` pour
  la copie. Définir `API_PRODUCTION_REGISTRY` et `API_PRODUCTION_API_SERVER`,
  valider les droits/configurations puis activer explicitement. Tout prérequis
  absent bloque la publication.

## Configuration applicative
Le chart staging fixe `API_DB_SEARCH_TIMEOUT_SECONDS=30` : il borne l'attente
du créneau de recherche, sans augmenter le pool de quatre connexions ni les
trois recherches SQL concurrentes par processus. L'acquisition de connexion
reste à deux secondes et l'exécution SQL à dix secondes. Le défaut applicatif
reste dix secondes ; l'override explicite accepte seulement une valeur finie
strictement positive et au plus trente secondes. Une attente plus longue ne
garantit pas la capacité à cinquante : elle doit être validée en charge réelle.
Les journaux distinguent les refus de file, expirations d'admission, de checkout,
de vérification de connexion et annulations SQL, sans texte de requête ni DSN.

Le chart utilise des valeurs revues propres à la cible et fixe explicitement `APP_ENV`. Le staging conserve ses paramètres actuels.

Namespace `assistant-rh-api`, release/service `assistant-rh-api`, URL interne `http://assistant-rh-api.assistant-rh-api.svc.cluster.local:8000`. Aucun Ingress. L'isolation réseau est gérée par Terraform et autorise Conversations.

Le secret `scaleway-database-ca` doit contenir `postgres.pem` (CA de la nouvelle base Scaleway). Le chart le monte en lecture seule ; `PGSSLMODE=verify-ca` et `PGSSLROOTCERT=/etc/scaleway-ca/postgres.pem` imposent la validation. Le DSN ne doit pas les désactiver.

Le secret `assistant-rh-api-runtime` doit contenir `SCW_POSTGRES_DSN` pour `10.10.1.2:5432/assistant_rh` avec le compte dédié, `ALBERT_API_KEY` et les paramètres d'identité/délégation requis par la version livrée ; `SCALEWAY_API_KEY` active le fallback. Ajouter au besoin les bases URL documentées par l'API. Le DSN historique de `scaleway-staging` cible une autre base et ne doit pas être repris automatiquement.

Le nouveau staging a été initialisé le 14/10/2026 : schéma complet, migrations API, corpus et paramètres RAG, sans groupes, sessions, runs ni feedbacks historiques. Le compte API est `readonly` avec le rôle `arh_api`, sans CREATE ni TRUNCATE. Le marqueur `assistant-rh-schema-ready` a été créé après contrôle SQL et `/healthz` contre la vraie base. Pour une reconstruction, reprendre le schéma/corpus et appliquer les migrations avec le compte migrateur dans une opération distincte ; le pipeline n'utilise jamais les fixtures synthétiques locales pour ce bootstrap. Après vérification réelle du schéma et des grants runtime, créer le ConfigMap marqueur `assistant-rh-schema-ready` dans ce namespace. Sans ce marqueur, le rollout refuse de démarrer. Le compte API ne doit pas servir à migrer. `/healthz` vérifie aussi la configuration ; la liveness TCP évite un cycle de redémarrages en cas de panne de base.
