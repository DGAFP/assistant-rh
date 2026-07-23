# Pages archivées

Pages Streamlit retirées de la navigation (`pages/`) car construites sur un
schéma de base qui n'existe plus sur staging/prod. Conservées ici pour
référence ; l'historique git fait foi.

| Page | Raison | Remplacée par |
|---|---|---|
| `07_Eval_Comparison.py` | Entièrement basée sur `goldset_runs` (ère RAGAS V1/V2), table absente des bases actuelles | `rag_quality_eval_runs` (`config_fingerprint`, `aggregate`, `run_label`) + journal d'évals via le skill run-rag-eval |
| `11_Golden_Beta_Analysis.py` | Analyse figée du beta-test jan26, basée sur `goldset_runs` | Feedback Dashboard (données vivantes `chat_feedbacks` × `chat_runs`) |

Pour réactiver une page : la déplacer dans `pages/` et réécrire ses requêtes
sur le schéma actuel.
