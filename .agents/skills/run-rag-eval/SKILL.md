---
name: run-rag-eval
description: Lance une eval de qualité RAG sur le goldset (staging), en run détaché reproductible, compare à une baseline, et CONSIGNE OBLIGATOIREMENT le paramétrage et les résultats dans le journal d'expérimentations. Use when the user wants to run a RAG eval, test a retrieval/generation change on the goldset, A/B two configs, or re-baseline after a pipeline change.
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
tester une variante sans muter la base, utiliser les overrides CLI
(`--selector-model`, `--section-rerank-top-k`), jamais un UPDATE en base pendant
qu'un autre run tourne.

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

Options utiles :
- `--ministry-scope per-question` (défaut) : chaque question MATTE/MSO/MI/MASA
  scopée sur SON ministère (comme l'app). `all` = pleinement granté (contamination
  inter-ministères). `none` = v3_tables seulement (mso/mi/masa invisibles).
- `--selector-model <modele>` / `--section-rerank-top-k <n>` : overrides A/B.
- `--skip-ragas` pour un run plus rapide sans RAGAS.

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

### 5. CONSIGNER dans le journal
Ajouter une section `## Run <id> — <label> (<date>)` à
`docs/evals/journal-experimentations-rag.md` :
- **Changements** vs run précédent (config, code mergé, goldset).
- **Résultats** : judge_pass global + par corpus, hit_rate, retrieval_gap_rate.
- **Lecture** : ce que ça prouve, les caveats (run contaminé si le goldset a bougé
  pendant le run ; variance du juge single-shot ; etc.).
Commiter le journal.

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
