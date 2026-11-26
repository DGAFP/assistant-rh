---
name: run-rag-eval
description: Lance une eval de qualité RAG sur le goldset (staging ou clone local), pipeline apps/api (--pipeline api) ou legacy, en run détaché reproductible, compare à une baseline, et CONSIGNE OBLIGATOIREMENT le paramétrage et les résultats dans le journal d'expérimentations. Use when the user wants to run a RAG eval, test a retrieval/generation change on the goldset, A/B two configs, or re-baseline after a pipeline change.
triggers:
  - lance une eval
  - run rag eval
  - teste sur le goldset
  - eval de qualité
  - re-baseline
  - A/B sur le goldset
category: evaluation
---

# Lancer une eval RAG (goldset)

Exécute `scripts/run_rag_quality_eval.py` sur le goldset `baseline_v1` (staging),
en **run détaché** (survit aux redémarrages de session), avec comparaison de
baseline, puis **consigne le run dans le journal**. C'est la règle d'équipe :
**un run lancé = une entrée dans `docs/evals/journal-experimentations-rag.md`**.

## Règle non négociable : consigner AVANT et APRÈS

Un chiffre d'eval sans son paramétrage est ininterprétable deux jours plus tard.
À chaque run :
1. **Avant** : noter dans le journal ce qui change vs le run précédent (config,
   goldset, code mergé) et le `--run-label` choisi.
2. **Après** : compléter l'entrée avec les résultats agrégés + la lecture.

Ne jamais lancer un run sans lui donner un `--run-label` explicite et parlant
(ex. `ab_sections20_20260706`, pas un timestamp anonyme).

## Choisir le pipeline : `--pipeline api` ou `legacy`

| Ce qui change | Pipeline | Pourquoi |
|---|---|---|
| Code d'`apps/api` (retrieval, reranking, sélection, builder, prompts côté API) | `--pipeline api` | C'est le pipeline servi en prod. Le legacy ne voit pas ces changements. |
| Config runtime, prompts ou corpus, pour décider d'une adoption en prod | `--pipeline api` | Mesure ce que les utilisateurs recevront. |
| Comparaison avec l'historique du journal (runs ≤ 218, baselines 19/52/206…) | `legacy` (défaut) | Ces runs ont tous été produits par le legacy. |
| Code de `packages/rag-pipeline` | `legacy` | Référence figée, à n'utiliser que pour l'historique. |

- `--pipeline api` construit le `Pipeline` d'`apps/api` comme l'API :
  `RAGConfigurationService` (lecture de `rag_config` sur le DSN de l'éval) puis
  adaptateurs DB et providers via `create_pipeline_factory`. Si `rag_config` est
  illisible, le run est refusé : l'éval ne mesure jamais les valeurs par défaut.
- Il exige `--ministry-scope per-question`, car l'API route toujours par ministère.
- Il exige `assistant_rh_api` dans l'environnement : `uv sync --all-packages --group dev`
  (même commande que la CI). Sinon l'import échoue avec ce message.
- Un run api porte `pipeline=api` dans son `eval_scope` et dans son empreinte de
  config. Il ne se compare qu'à un autre run api : une baseline legacy est
  signalée `not_comparable`. **Pour qualifier une PR `apps/api`, rejouer `dev`
  en `--pipeline api`** au lieu de reprendre une baseline legacy.
- Le `.env` racine définit `PGHOST`/`PGUSER`… que la couche DB d'`apps/api`
  refuse. Comme le DSN de l'éval est explicite, le mode api les retire du process
  et l'indique dans le log (`variables libpq ambiantes ignorées`).

## Pré-requis

- CWD = worktree de la branche testée (le code importé DOIT porter les changements
  à mesurer). Vérifier : `git -C . branch --show-current`.
- `.env` chargé avec `SCW_POSTGRES_DSN_STAGING`, `ALBERT_API_KEY`,
  `SCALEWAY_API_KEY`. **Piège** : la ligne `DATABASE_URL` du `.env` racine est
  malformée et casse `source .env && …` — toujours passer par le launcher ci-dessous.
- Judge : `SCALEWAY_JUDGE_MODEL` (défaut `qwen3-235b-a22b-instruct-2507`).

## Procédure

### 1. Choisir la baseline
Trouver le dernier run full comparable (même panel, même régime de goldset) :
```bash
# runs full récents (100 questions), pas les smokes limit-5
psql "$SCW_POSTGRES_DSN_STAGING" -c "SELECT id, run_label, status, aggregate->>'judge_pass_rate'
  FROM rag_quality_eval_runs WHERE goldset_name IN ('baseline_v1','post_ingestion_refactor')
  ORDER BY created_at DESC LIMIT 8"
```
Run 52 (`post_ingestion_refactor_20260705`) et run 19 (`baseline_v1_canonical_20260629`)
sont les jalons de référence. Voir le journal pour l'historique complet.

### 2. Écrire un launcher détaché
La config runtime (`rag_config`, ligne unique en base) est **partagée** — pour
tester une variante sans muter la base, utiliser les overrides CLI (liste
ci-dessous), jamais un UPDATE en base pendant qu'un autre run tourne. Les
overrides s'appliquent en mémoire, sont tracés dans `config_adjustments` et
entrent dans l'empreinte de config. Ablation stricte : **un seul override par
screening**.

```bash
SCRATCH=<scratchpad>
cat > $SCRATCH/launch_eval.sh <<'EOF'
#!/bin/zsh
cd <worktree-abs-path> || exit 1
set -a; source .env 2>/dev/null; set +a
export SCW_POSTGRES_DSN="$(echo $SCW_POSTGRES_DSN_STAGING | tr -d '"')"
export SCALEWAY_JUDGE_MODEL="${SCALEWAY_JUDGE_MODEL:-qwen3-235b-a22b-instruct-2507}"
echo "cwd=$(pwd)"
exec uv run --no-sync python scripts/run_rag_quality_eval.py \
  --pipeline api \
  --goldset-name <label> --any-goldset --tag baseline_v1 \
  --record-db --dedupe-scope config \
  --baseline-run-id <N> \
  --ministry-scope per-question \
  --run-label "<label>" \
  --output-dir "$SCRATCH/eval-<label>"
EOF
chmod +x $SCRATCH/launch_eval.sh
nohup $SCRATCH/launch_eval.sh > $SCRATCH/eval-<label>.log 2>&1 &
```

Sur le clone local (`docs/LOCAL_DEV.md`, conteneur `assistant-rh-local-pg`),
remplacer l'export du DSN staging par `--dsn-env SCW_POSTGRES_DSN_LOCAL`. La base
doit être **inscriptible** pour que `--record-db` enregistre le run : pas de
clone en `default_transaction_read_only`.

Options utiles :
- `--pipeline api|legacy` : voir « Choisir le pipeline ». `legacy` par défaut.
- `--ministry-scope per-question` (défaut) : chaque question MATTE/MSO/MI/MASA
  scopée sur SON ministère (comme l'app). `all` = pleinement granté (contamination
  inter-ministères). `none` = v3_tables seulement (mso/mi/masa invisibles).
  `--pipeline api` n'accepte que `per-question`.
- Overrides nommés (les deux pipelines) : `--selector-model`, `--selector-provider`,
  `--generator-model`, `--generator-provider`, `--system-prompt-name`,
  `--section-rerank-top-k`, `--rerank-input-k`, `--initial-top-k`,
  `--ivfflat-probes`, `--min-kept-sections`, `--doc-entire-threshold-wide`.
- `--reranker-union-top-k <n>` : union reranker du sélecteur (`0` = pas d'union).
  N'existe que dans un `apps/api` qui porte ce champ (PR #644). Sinon, et en
  legacy, refus explicite (`champ inconnu`) plutôt qu'un override ignoré.
- `--config-override SECTION.CHAMP=VALEUR` (répétable) : n'importe quelle clé de
  la config imbriquée, ex. `aggregation.rerank_input_k=40`,
  `context.token_budget=9000`. Le type vient de la valeur courante. Un champ
  inconnu, une valeur mal typée ou une clé surchargée deux fois arrêtent le run
  avant le premier appel.
- `--skip-ragas` pour un run plus rapide sans RAGAS (voir « RAGAS séparé »).

### 3. Attendre en détaché (jamais bloquer)
Ne pas chaîner de `sleep`. Poser une vigie `until` sur le statut en base :
```bash
until [ "$(psql "$SCW_POSTGRES_DSN_STAGING" -tAc \
  "SELECT status FROM rag_quality_eval_runs WHERE run_label='<label>'")" != "running" ]; do
  sleep 120; done
```
Un run 100 questions avec judge + RAGAS ≈ 1 h–1 h 30.

### 4. Lire les résultats et comparer
```bash
psql "$SCW_POSTGRES_DSN_STAGING" -c "SELECT
  q.source, count(*),
  round(avg(CASE WHEN i.judge_result->>'pass'='true' THEN 1.0 ELSE 0.0 END)::numeric,2) AS pass,
  round(avg((i.deterministic_metrics->>'hit_rate')::float)::numeric,2) AS hit
  FROM rag_quality_eval_items i JOIN goldset_questions_v2 q ON q.id=i.question_id
  WHERE i.run_id=<id> GROUP BY q.source ORDER BY 2 DESC"
```
Regarder aussi `aggregate->>'judge_pass_rate'` et `aggregate->>'retrieval_gap_rate'`.
Comparer par corpus, pas seulement le global (un corpus modifié peut progresser
pendant que le global stagne à cause d'artefacts goldset ou de la variance du juge).

Comparer **par cas**, pas seulement l'agrégat. Lister les verdicts qui basculent :
```bash
psql "$DSN" -c "SELECT b.question_id,
  b.judge_result->>'pass' AS base, c.judge_result->>'pass' AS cand,
  (b.deterministic_metrics->>'hit_rate') AS base_hit, (c.deterministic_metrics->>'hit_rate') AS cand_hit
  FROM rag_quality_eval_items b JOIN rag_quality_eval_items c USING (question_id)
  WHERE b.run_id=<base> AND c.run_id=<cand>
    AND (b.judge_result->>'pass') IS DISTINCT FROM (c.judge_result->>'pass')
  ORDER BY b.question_id"
```
Pour chaque bascule, regarder `deterministic_metrics->'stages'` (étage où le gold
est perdu : `pool`, `sections_top12/20`, `selector_kept`,
`context_builder_output`) et savoir si le contexte servi a changé. Une bascule à
contexte identique relève du bruit du générateur ou du juge.

### 4 bis. Protocole d'adoption : votes, plancher de bruit, seuil de 5 points

Pour décider d'un merge ou d'une adoption, et pas seulement pour un screening :

1. **Votes multiples** : `--judge-votes 3` (majorité de 3) sur le juge souverain
   Scaleway. Un run maj-3 n'est comparable qu'à un run maj-3 (`judge_votes` est
   dans l'`eval_scope`). Le screening intermédiaire peut rester à 1 vote.
2. **RAGAS séparé** : lancer le panel avec `--skip-ragas`, puis enrichir le run
   enregistré sans rejouer le pipeline :
   `uv run python scripts/backfill_ragas.py --run-label <label> --dsn-env <DSN_ENV>`
   (reprenable, les items déjà complétés sont sautés).
3. **Plancher de bruit** : rejouer `dev` **deux fois**, même pipeline, même DSN,
   même panel et même nombre de votes (`<label>_dev_a`, `<label>_dev_b`, depuis un
   worktree sur `dev`). Le nombre de verdicts qui basculent entre les deux rejeux
   (requête par cas ci-dessus) et l'écart de `judge_pass_rate` forment le plancher.
   Un écart candidat/`dev` inférieur à ce plancher n'est pas un signal. Repère
   (PR #644, 25/11) : un rejeu de `dev` sur 32 questions change 5 verdicts, dont
   3 à contexte identique.
4. **Seuil de recul de 5 points** : le candidat ne doit pas perdre plus de
   5 points de `judge_pass_rate` face à `dev`. Le gate est intégré :
   `--baseline-run-id <dev_a> --gate-against-baseline` (défaut
   `--max-judge-pass-rate-drop 0.05`, idem `--max-doc-recall-drop`). Au-delà, statut
   `failed_quality_gate` et code de sortie 1 : la variante n'est pas mergée en
   l'état. Sous le seuil mais au-dessus du plancher : analyser les bascules par cas
   avant de conclure.

### 5. CONSIGNER dans le journal
Ajouter une section `## Run <id> — <label> (<date>)` à
`docs/evals/journal-experimentations-rag.md` :
- **Changements** vs run précédent (pipeline `api`/`legacy`, config et overrides,
  code mergé, goldset).
- **Résultats** : judge_pass global + par corpus, hit_rate, retrieval_gap_rate.
- **Lecture** : ce que ça prouve, les caveats (run contaminé si le goldset a bougé
  pendant le run ; variance du juge single-shot ; etc.).
Commiter le journal.

## Pièges appris (25/11/2026)

- **Qualifier `apps/api` avec le legacy** ne mesure rien : la PR #644 a dû passer
  par des lanceurs ad hoc, sans suivi dans `rag_quality_eval_runs`. Utiliser
  `--pipeline api`.
- **Baseline d'un autre pipeline** : un run api comparé à une baseline legacy est
  `not_comparable`. Rejouer `dev` en api.

## Pièges appris (06/07/2026)

- **CWD** : le CWD Bash retombe sur la checkout principale entre appels — toujours
  `cd <worktree>` en tête du launcher, sinon `uv` importe le code SANS les modifs.
- **Modèle non déterministe** : `gpt-oss-120b` (générateur ET sélecteur) varie à
  temp 0. Ne jamais conclure sur un single-shot ; comparer à périmètre constant.
- **Goldset figé pendant un run** : ne pas modifier `goldset_questions_v2` pendant
  qu'un run tourne, sinon ses items mélangent deux régimes (run contaminé — cf.
  run 54). Corriger le goldset AVANT, puis lancer.
- **`retrieval_gap` ≠ faute de génération** : depuis le 06/07 le cap retrieval est
  soft ; un hit_rate=0 est un diagnostic pipeline/goldset, pas un échec de réponse.
