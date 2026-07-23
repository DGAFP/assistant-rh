# Verdicts officiels — juge Scaleway qwen3-235b, vote majoritaire à 3

Verdicts par question des runs baseline **118 / 123 / 124** (goldset curé 99 questions),
re-jugés offline avec le juge officiel souverain (`qwen3-235b-a22b-instruct-2507`,
Scaleway, 3 votes indépendants par réponse — 891 votes au total, ~1,5 €).

Ils constituent la **référence officielle 0,677** (67/99) et la liste des
**26 échecs stables** qui servent de cibles aux gates d'adoption
(cf. `../journal-experimentations-rag.md`, section « Référence officielle » du 22/07,
et issue #336).

## Format

Un fichier par run : `official_scw_maj3_<run_id>.json`.

```json
{ "<question_id>": { "votes": [true, false, true], "pass": true } }
```

- `votes` : les 3 verdicts indépendants du juge (ordre d'appel).
- `pass` : verdict majoritaire (≥2/3).

## Provenance

Produits le 22/07/2026 sur la VM pont (`~/assistant-rh/rejudge_grok/`) par le script
`rejudge_official.py` (client HTTP timeout 45 s), à partir des réponses stockées en
base (`rag_quality_eval_items`) — les verdicts qwen d'origine restent en base,
inchangés. Versionnés ici pour que toute lecture appariée d'un gate reste
reproductible sans dépendre d'artefacts éphémères de la VM.
