# Rôles d'exécution PostgreSQL (Scaleway)

> Runbook de [#599](https://github.com/DGAFP/assistant-rh/issues/599), livré par la migration `supabase/migrations/20261007130000_api_runtime_roles.sql`. Objectif : seule l'API écrit le contenu des feedbacks individuels ; aucun runtime ne peut désactiver les garde-fous.

## Modèle

| Identité | Type | Rôle | Droits |
|---|---|---|---|
| `assistant_rh` | utilisateur Scaleway existant | administrateur, migrations (`supabase db push`) | propriétaire des tables ; peut tout contourner, par une action d'administration explicite |
| `assistant_rh_streamlit` | utilisateur Scaleway, permission `readonly` | runtime Streamlit | + `arh_streamlit` : écriture de ses tables et de l'historique collectif |
| `assistant_rh_api` | utilisateur Scaleway, permission `readonly` | runtime API | + `arh_api` : seul écrivain du contenu des feedbacks individuels |
| `assistant_rh_ingest` | utilisateur Scaleway existant | ingestion, analyse IA | + `arh_analysis` : colonnes `error_category`, `ai_reason`, `ai_analyzed_at` ; plus d'écriture sur `chat_runs`, `chat_feedbacks`, `chat_reviews` |

Les rôles `arh_*` sont `NOLOGIN` et ne possèdent rien. La permission Scaleway `readonly` fournit l'usage du schéma, la lecture et l'exécution des fonctions d'extension (vector, pg_trgm) ; `assistant_rh` ne peut pas les accorder lui-même, car le schéma `public` appartient à `_rdb_superadmin`. `api_attach_runtime_user()` retire ensuite toute écriture directe (y compris `TRUNCATE`) et l'accès aux tables `api_*` pour Streamlit, puis accorde le rôle `arh_*`.

Le trigger `api_feedback_individual_guard` refuse, sur un run individuel (`author_user_id` non nul), toute insertion, modification de contenu ou suppression qui ne vient pas de `arh_api`. Les colonnes `beta_scope`, `theme` et d'analyse IA restent modifiables ; le propriétaire peut supprimer (purge). Un runtime n'étant propriétaire d'aucune table, il ne peut ni désactiver ni supprimer ce trigger.

L'identifiant `turn_id` d'un run individuel est également immuable : renommer temporairement le parent ne permet pas de contourner le garde, même sans clé étrangère sur les feedbacks. Un feedback individuel orphelin reste protégé contre les modifications de contenu hors API.

**Limite** : `assistant_rh` (et `_rdb_admin`) peuvent désactiver le trigger ou se donner un rôle. C'est un accès administratif, réservé aux migrations et à l'exploitation.

## Vérifié avant livraison

Sur une copie du **schéma** staging du 2026-10-07 (dump sans données, mêmes rôles et ACL, conteneur jetable), migrations appliquées par `assistant_rh` non superutilisateur, utilisateurs simulés dans le pire cas (`readwrite` avec `TRUNCATE`) : 92 contrôles réussis.

- API : stores réels (sessions, quotas, anti-rejeu, recherche vectorielle/lexicale/titres sur les 7 sources, contenus, runs individuels, feedbacks et audit).
- Streamlit : fonctions réelles de démarrage (DDL conditionnel), groupes (création, mise à jour, mot de passe, suppression), configuration, prompts, acronymes, page d'évaluation d'intention, upsert `chat_logger`, feedback legacy et son archivage, revues, analyse IA, lectures des tableaux de bord.
- Refus : désactivation ou suppression du trigger, `TRUNCATE`, modification/insertion/suppression d'un feedback individuel, changement d'auteur, lecture de `api_sessions` par Streamlit, écritures hors périmètre de l'API et de l'ingestion.
- Rollback puis réapplication.

**Reste à vérifier sur staging** : la permission `readonly` réelle de Scaleway (les GRANT exacts ne sont pas documentés) et le parcours Streamlit complet.

Dérives de schéma staging relevées au passage, indépendantes des rôles (échouent aussi pour le propriétaire) : pas de contrainte unique sur `acronyms.acronym` (`add_acronym` échoue), pas de colonne `chat_reviews.updated_at` (l'upsert des revues échoue), `chat_runs.conversation_id` en `varchar(8)` (trop court pour l'identifiant de corrélation écrit par l'API).

## Procédure (staging, puis production)

1. **Migrations** : la promotion `dev → staging` applique `20261007120000_api_individual_identity` et `20261007130000_api_runtime_roles` (`db-migrations-scaleway.yml`). Rien ne change pour les runtimes tant qu'ils utilisent le DSN admin.
2. **Utilisateur Streamlit** :
   ```bash
   scripts/create_scaleway_db_runtime_user.sh staging streamlit
   ```
   Le script crée `assistant_rh_streamlit` (`readonly`) et enregistre `STREAMLIT_POSTGRES_DSN` dans l'environnement GitHub `scaleway-staging`, sans afficher le mot de passe.
3. **Rattachement**, en tant qu'`assistant_rh` sur staging :
   ```sql
   SELECT public.api_attach_runtime_user('assistant_rh_streamlit', 'arh_streamlit');
   ```
4. **Contrôle** en lecture seule :
   ```sql
   SELECT pg_has_role('assistant_rh_streamlit', 'arh_streamlit', 'USAGE') AS role,
          has_table_privilege('assistant_rh_streamlit', 'public.chat_feedbacks', 'TRUNCATE') AS truncate,
          has_table_privilege('assistant_rh_streamlit', 'public.api_sessions', 'SELECT') AS sessions,
          has_function_privilege('assistant_rh_streamlit', 'public.vector_in(cstring, oid, integer)', 'EXECUTE') AS vector;
   ```
   Attendu : `t`, `f`, `f`, `t`.
5. **Bascule** : relancer `streamlit-deploy-staging.yml`. Le conteneur reçoit `STREAMLIT_POSTGRES_DSN` dans `SCW_POSTGRES_DSN` ; `supabase db push` continue d'utiliser le DSN admin.
6. **Recette manuelle** : connexion de groupe, question, feedback (puis second feedback sur la même réponse), administration (groupes, configuration, prompts, acronymes), tableaux de bord, explorateur goldset, page d'évaluation d'intention.
7. **API** (quand elle sera déployée) : `scripts/create_scaleway_db_runtime_user.sh staging api`, puis `SELECT public.api_attach_runtime_user('assistant_rh_api', 'arh_api');`. Ne configurer `CONVERSATIONS_DELEGATION_*` qu'ensuite.
8. **Production** : mêmes étapes après validation staging.

Après toute modification de la permission Scaleway d'un utilisateur (qui réapplique ses GRANT directs), relancer l'étape 3. Une nouvelle table créée par migration est lisible par Streamlit (privilèges par défaut) ; toute écriture et tout accès de l'API doivent être accordés explicitement dans la migration.

## Retour arrière

- **Streamlit** : supprimer le secret `STREAMLIT_POSTGRES_DSN` de l'environnement et relancer le déploiement ; le conteneur revient au DSN admin.
- **Rôles** : après avoir rebasculé les runtimes, exécuter `supabase/rollback/20261007130000_api_runtime_roles.down.sql` en tant qu'`assistant_rh` (restaure le garde D1 et les droits d'ingestion, détache les utilisateurs, supprime les rôles). La migration reste dans l'historique Supabase ; la réappliquer plus tard est idempotent. Supprimer ensuite les utilisateurs Scaleway si besoin.
