# LEDGER — journal du chantier hexagonal-split

> Journal append-only. **Chaque PR du chantier amende ce fichier.** C'est le point d'entrée pour connaître l'état du chantier sans lire les PRs.
> Trois sections : Avancement · Reports depuis le runtime existant (dette de parité, vide aux jalons M1/M2/M3/M4) · Améliorations pour après le merge.

## Avancement

| Date | PR / jalon | Quoi |
|---|---|---|
| 2026-08-21 | PR 0 | Plan validé (grilling) ; docs `docs/architecture/hexagonal-split/` créées. Prochaine étape : jalon M0 (re-baseline goldset) puis création de `feat/hexagonal-api`. |
| 2026-08-25 | PR 0 — amendement | Revue d'architecture intégrée : `packages/rag-core` séparé, reconstruction parallèle sur `dev`, audit d'isolation B0 élargi, état par requête/SSE spécifiés, spikes `conversations` + Scaleway avancés en phase 0, API dark, bascule Streamlit sous feature flag, preuves déterministe/live séparées et suppression de l'ancien runtime reportée après stabilité. |
| 2026-08-25 | PR 0 — second amendement | Feedback de revue intégré : décisions sorties de l'overview et catégorisées ; core replacé dans `assistant_rh_api/core` ; prompts reliés explicitement au pipeline ; ordre DB/adaptateurs → auth → models → completion par étapes ; extraction comportementale plutôt que déplacement de fonctions ; auth admin alignée sur les groupes/rôles ; seulement local + VM homelab avant le premier déploiement Scaleway staging. |
| 2026-08-26 | PR 0 — lisibilité | Contexte, architecture cible, contrat et six séquences conservés ; répétitions raccourcies, préparation regroupée sans retirer les étapes DB/adaptateurs puis extraction ; premier Mermaid corrigé (point-virgule non échappé). |
| 2026-09-01 | A1 — [PR #445](https://github.com/DGAFP/assistant-rh/pull/445), [issue #440](https://github.com/DGAFP/assistant-rh/issues/440) | Ancien pipeline TypeScript, dépendances, skill, workflows et documentation active supprimés. Les fixtures, contrats et outils Python/génériques de conformance sont conservés pour la reconstruction API ; aucun changement du runtime RAG servi. |
| 2026-09-01 | #439 — jalon M0a/M0b | Références de parité figées au commit `9bf1cf0` sans réglage qualité : run live #240, 98/98 sans erreur et métriques globales identiques à #230 ; [preuve M0a](../../evals/evidence/m0a_api_parity_dev_20260901.json) `ead6beec…` et [journal](../../evals/journal-experimentations-rag.md). Bundle M0b exact 7 fixtures / 56 artefacts, [mode d'emploi](../../../tests/conformance/M0_REPLAYS.md) et [manifest](../../../tests/conformance/baselines/m0-api-parity-dev-9bf1cf0/manifest.json) `f5e9ffef…`, vérifiés offline. q214/q676 restent incluses et la dette goldset #421 demeure hors jalon. |
| 2026-09-01 | A4/A6 — [PR #447](https://github.com/DGAFP/assistant-rh/pull/447), [issue #441](https://github.com/DGAFP/assistant-rh/issues/441) | Squelette installable `apps/api`, frontières `core`/`handlers`/`db`/`gateways`, probe `/healthz` sans initialisation RAG/provider, gardes import-linter, image API et base locale pgvector strictement synthétique ajoutés. Aucun changement du runtime RAG servi. |
| 2026-09-02 | A4/A6 — [PR #448](https://github.com/DGAFP/assistant-rh/pull/448), suivi de [#447](https://github.com/DGAFP/assistant-rh/pull/447) | Assets API regroupés sous `docker/api`, stack locale API + PostgreSQL synthétique lançable par `moon run api:local`, Moon 2.5.3 épinglé via Proto et tâches locales Moon validées. Le probe conteneurisé répond `200` sans accès aux données de staging. |
| 2026-09-02 | A4/A6 — [PR #450](https://github.com/DGAFP/assistant-rh/pull/450), suivi de [#447](https://github.com/DGAFP/assistant-rh/pull/447) | Hook `pre-push` ajouté pour exécuter les checks Moon ciblés de l'API (`smoke`, lint, frontières d'imports et tests) avant envoi ; le test PostgreSQL synthétique complet reste obligatoire en CI. |

## Reports depuis le runtime existant

> Format : date · commit/PR du runtime existant · fichiers touchés · reporté vers `assistant_rh_api.core` · tests/preuve · statut (`reporté` / `à reporter`).

_(vide — aucun report en attente)_

## Améliorations notées pour après le merge

> Idées d'amélioration pipeline/API survenues pendant la reconstruction — interdites dans le chantier (iso-fonctionnel), à instruire après.

_(vide)_
