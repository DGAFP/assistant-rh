# Recette locale Conversations → Assistant RH — 12 octobre 2026

Issue [#596](https://github.com/DGAFP/assistant-rh/issues/596),
[PR fork #1](https://github.com/DGAFP/conversations/pull/1) et
[PR API #612](https://github.com/DGAFP/assistant-rh/pull/612).

## Périmètre exécuté

Les **deux applications réelles communiquent par HTTP**, avec les sessions, les
contrôles d’admission, la signature du fork, le vérificateur API, le pipeline RAG,
les adaptateurs SQL et la persistance des runs. Deux processus API distincts
partagent la table anti-rejeu PostgreSQL. Aucun transport simulé ne remplace la
liaison Conversations/API, ni les stores d’autorisation ou de runs.

- Fork : `b3d3018a93759515b0c828f58fb8d9c1d9182e78`.
- Code API et scénario : `bff6356`.
- Conversations : Django/WSGI, sessions en base et cache local ; Python 3.14.
- API : point d’entrée standard Uvicorn, deux processus ; Python 3.12.
- PostgreSQL jetable `pgvector/pgvector:pg17`, deux bases distinctes ; schéma RAG
  synthétique et migrations du manifeste `docker/api/bootstrap-local.txt`,
  migrations Django du fork. L’API utilise un login non propriétaire rattaché
  à `arh_api` par `api_attach_runtime_user`. Le compte propriétaire sert au
  bootstrap, aux fixtures et à la base Django dans cette recette locale.
- Aucun `.env` de développeur, dump, compte réel ou service distant utilisé.
  Les services sont liés à `127.0.0.1`, les données sont synthétiques.

Deux parcours exécutent le même scénario : **mot de passe local réel, sans
configuration OIDC**, puis OIDC avec fournisseur simulé. En mode mot de passe,
seuls les services IA sont simulés. Les comptes locaux ont un mot de passe haché,
aucun sujet OIDC et aucun droit staff/superutilisateur ; le navigateur HTTP envoie
le formulaire et son CSRF, puis utilise la session Django normale.

Dans le parcours OIDC, le fournisseur
local réalise le flux code, le nonce, les jetons RS256, le JWKS, userinfo et le
retour de logout. Les endpoints Albert/Scaleway locaux servent embeddings,
reranking et réponses déterministes. Ils utilisent HTTPS avec un certificat
local explicitement approuvé ; la vérification TLS reste active. Le lancement
API du scénario change uniquement le bundle de confiance du processus pour
ce certificat, puis appelle son point d’entrée normal.

## Résultats

Les preuves [mot de passe](evidence/596-password-recipe.json) et
[OIDC](evidence/596-oidc-recipe.json) consignent les révisions, les heures,
les dix contrôles réussis pour chacun des modes et les appels aux fournisseurs
synthétiques. La [première recette](evidence/596-local-recipe.json) reste archivée.

| Contrôle | Résultat |
|---|---|
| Authentification et admission | Compte initialement refusé, puis admis ; UUID distincts pour Alice et Bob dans les deux modes |
| Mot de passe local | Connexion sans fournisseur OIDC, comptes ordinaires, mauvais mot de passe et POST sans CSRF refusés, connexion silencieuse désactivée |
| Catalogue | Deux corpus autorisés ; modèle hors périmètre refusé côté serveur |
| Chat JSON et SSE | Appels réels depuis Conversations ; réponses reçues et runs `completed` en base |
| Auteur et sources | Auteur du run = UUID Conversations ; sources MATTE/MSO dans le bon ministère |
| Isolation | Même groupe, mais conversations et feedback d’autrui inaccessibles |
| Secours fournisseur | Échec Albert simulé, réponse Scaleway ; auteur et ministère MSO conservés |
| Retrait/rétablissement | Catalogue actualisé, nouvelle question refusée, historique masqué puis restauré |
| Anti-rejeu partagé | Assertion du vrai signataire acceptée par une API, refusée par l’autre |
| Sessions | Logout local avec confirmation/CSRF ou logout OIDC complet, UUID stable à la reconnexion, nouvel audit HMAC, anciennes sessions révoquées après désactivation/réactivation, expiration refusée |
| Corrélation | Même identifiant visible dans les journaux des deux services, sans assertion signée |

Les appels de feedback utilisent les délégations émises par le signataire du
fork pour ses sessions réelles. Ils appellent directement les routes API ; cela
ne constitue pas le raccordement des boutons de feedback Conversations (#595).
L’admission et les modifications de droits sont provisionnées par l’ORM dans
les fixtures ; cette recette ne teste pas l’interface d’administration au navigateur.

Le premier passage a révélé l’absence des logs applicatifs INFO dans le point
d’entrée standard de l’API. La configuration Uvicorn inclut maintenant le logger
`assistant_rh_api`. Les 25 tests ciblés du point d’entrée, de l’authentification,
de la corrélation et du streaming passent après correction. Ruff et le formatage
passent également. Pour l'ajout du mot de passe : 134 tests backend distincts
couvrent RH, authentification, utilisateurs, configuration et settings (132
réussis dans le passage groupé, puis les 27 tests de configuration réussis après
ajout du nouveau champ attendu dans les assertions synchrones et asynchrones).
Les 10 tests frontend d'authentification/configuration, TypeScript, ESLint,
Ruff et Pylint (10/10) passent.

## Reproduire

Prérequis : Docker, `.venv` API (`uv sync --package assistant-rh-api --group dev`)
et `.venv` backend Conversations (`uv sync --extra dev` dans `src/backend`).
Depuis le worktree Assistant RH :

```bash
.venv/bin/python scripts/integration/conversations_identity.py \
  --conversations /chemin/vers/le/fork-conversations \
  --auth password \
  --output /tmp/recette-identite-596-password

.venv/bin/python scripts/integration/conversations_identity.py \
  --conversations /chemin/vers/le/fork-conversations \
  --auth oidc \
  --output /tmp/recette-identite-596-oidc
```

Le dossier de sortie doit être nouveau. Le script refuse les variables libpq
ambiantes pouvant rediriger la base, crée son propre conteneur à nom unique et
alloue des ports locaux libres. Il arrête ses processus et son conteneur en fin
de recette, y compris en cas d’échec, puis supprime les clés privées temporaires.
Les logs et `results.json` restent dans le dossier de sortie ; un code de sortie
non nul ou `success: false` signifie que la recette a échoué.

Pour activer le parcours temporaire dans le fork : `ASSISTANT_RH_ENABLED=true`
et `ASSISTANT_RH_LOCAL_LOGIN_ENABLED=true`, puis création/admission des comptes
ordinaires dans l'administration Django. Le
[runbook du fork](https://github.com/DGAFP/conversations/blob/codex/596-individual-identity/docs/assistant-rh/identity.md)
décrit les réglages, le formulaire, la déconnexion et le futur rattachement OIDC.

## Ce que cette recette ne valide pas

Elle valide l’intégration locale des backends, pas une activation de production.
Restent la connexion au **vrai ProConnect**, les vrais fournisseurs IA, les règles
réseau et grants de l’environnement cible, la configuration Redis/ASGI de cible,
les écrans navigateur, le proxy documentaire #594 et l’interface de feedback #595.
Les fixtures SQL ne remplacent pas la vérification des migrations sur le schéma
réel. L’issue #596 reste ouverte pour cette recette cible et ses dépendances.
