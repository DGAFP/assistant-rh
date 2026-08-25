# LEDGER — journal du chantier hexagonal-split

> Journal append-only. **Chaque PR du chantier amende ce fichier.** C'est le point d'entrée pour connaître l'état du chantier sans lire les PRs.
> Trois sections : Avancement · Reports depuis le runtime existant (dette de parité, vide aux jalons M1/M2/M3/M4) · Améliorations pour après le merge.

## Avancement

| Date | PR / jalon | Quoi |
|---|---|---|
| 2026-08-21 | PR 0 | Plan validé (grilling) ; docs `docs/architecture/hexagonal-split/` créées. Prochaine étape : jalon M0 (re-baseline goldset) puis création de `feat/hexagonal-api`. |
| 2026-08-25 | PR 0 — amendement | Revue d'architecture intégrée : `packages/rag-core` séparé, reconstruction parallèle sur `dev`, audit d'isolation B0 élargi, état par requête/SSE spécifiés, spikes `conversations` + Scaleway avancés en phase 0, API dark, bascule Streamlit sous feature flag, preuves déterministe/live séparées et suppression de l'ancien runtime reportée après stabilité. |

## Reports depuis le runtime existant

> Format : date · commit/PR du runtime existant · fichiers touchés · reporté vers `packages/rag-core` · tests/preuve · statut (`reporté` / `à reporter`).

_(vide — aucun report en attente)_

## Améliorations notées pour après le merge

> Idées d'amélioration pipeline/API survenues pendant la reconstruction — interdites dans le chantier (iso-fonctionnel), à instruire après.

_(vide)_
