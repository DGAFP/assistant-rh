# M1 — parité moteur et écritures des runs

21 septembre 2026 · [#465](https://github.com/DGAFP/assistant-rh/issues/465)
· prérequis C7 : [PR #579](https://github.com/DGAFP/assistant-rh/pull/579).

## Audit des écritures

L'audit initial lit les 200 derniers runs historiques et les 1 400 derniers
événements de staging, sans écrire ni copier les conversations. Il compare
leurs colonnes et taux de remplissage au run API effectué sur la copie locale
du corpus. Les nouveaux essais écrivent uniquement sur PostgreSQL local.

| Mesure | Historique observé | API avant correction | Correction |
|---|---|---|---|
| Provider et modèle de génération | 200 providers renseignés ; attribution incorrecte possible après fallback | `model` contenait le modèle public API, provider SQL vide | Provider/modèle effectivement utilisés dans les colonnes historiques ; modèle public conservé dans `api_record.model` |
| Latence totale | 199/200 valeurs positives | Absente | Mesure monotone de l'exécution, avant finalisation DB |
| Durées des étapes | Colonnes `v3_*_ms` et événements | Présentes dans les événements seulement | Projection vers les colonnes historiques ; durées des retries additionnées |
| Temps au premier token | 196/200 valeurs positives ; mesuré depuis le début de génération | Non mesuré | Mesures distinctes depuis le début du run et depuis le début de génération ; `null` en non-stream |
| Comptages retrieval/contexte | 199/200 runs renseignés | Dans les traces détaillées, colonnes SQL vides | Compteurs dédiés avant troncature des traces, également projetés dans les colonnes historiques |
| Usage LLM | Estimation de longueur de réponse ; pas de compteurs réels complets | Usage dans les diagnostics détaillés | Compteurs déclarés par le provider pour chaque appel intent/selector/génération ; usage absent conservé `null` |
| Environnement des traces | `staging` pour les 1 400 événements échantillonnés | Chaîne vide pour les sept événements du run local | Environnement fourni par le bootstrap, `production` normalisé en `prod` |
| Statut du run | Pas de statut terminal structuré commun ; écriture best effort | `completed`, `failed`, `cancelled` dans `api_record` | Garantie atomique run/sources/traces conservée ; aucune réécriture des anciens runs |

Les six étapes métier disposent maintenant de métriques compactes, indépendantes
des textes et listes de diagnostic tronqués. La configuration conserve sa révision
et son snapshot dans la trace existante. Le core mesure ; l'adaptateur PostgreSQL
projette vers le schéma historique. Il n'importe ni logger Streamlit ni SQL.

Les colonnes de résumé utilisées existent déjà en staging. Le schéma synthétique
des tests API était partiel ; il a été complété pour exercer leurs écritures.
Le clone local a reçu les colonnes historiques manquantes, sans importer les
anciens chats. Aucune migration distante n'est nécessaire pour ces corrections.

## Lecture correcte des métriques

- `api_record.metrics.elapsed_ms` inclut configuration et pipeline, jusqu'avant
  la finalisation. Ce n'est pas la durée HTTP complète avec commit et réseau.
- `api_record.metrics.first_token_ms` mesure le premier delta observé par le
  core depuis le début du run. `generation_first_token_ms` commence à l'étape
  générateur ; c'est cette seconde mesure qui alimente `v3_ttft_ms`/`ttft_ms`
  pour conserver la définition historique. Aucun ping SSE n'est un token.
- L'usage se lit dans `rag_trace_events.metrics` : `usage_known`,
  `prompt_tokens`, `completion_tokens`, `total_tokens`, `provider`, `model`.
  Les appels de retry restent séparés par `attempt_name`. Une absence de
  compteur n'est pas un coût nul. L'usage OpenAI renvoyé par l'API conserve
  son contrat existant, limité à la génération.
- `sources_used_count` mesure les sources servies par chaque transport.
  Le transport historique compte ses éléments de contexte affichés, C1
  déduplique les références documentaires : ces deux nombres ne prouvent pas
  seuls une différence de retrieval. Comparer aussi les documents et les
  éléments de contexte via les artefacts d'évaluation.

Restent hors de cette correction : coût complet de tous les providers,
tokens d'embedding/reranking et appels interrompus quand aucun compteur n'est
retourné, temps du commit, latence réseau client, files d'attente/saturation et
échecs HTTP avant création du run. Ces mesures relèvent de l'opérabilité D4/M2.
Les tokens exacts, statuts et timings qui n'ont jamais été enregistrés dans les
anciens runs ne peuvent pas être reconstitués fidèlement.

## Preuves et protocole M1

Code mesuré : `16e6afe`. Les sept scénarios du compagnon M0b restent exacts,
sur 27 sorties d'étapes et sept résultats finaux ; cinq mutations négatives
sont détectées. La suite API valide 963 tests distincts (962 lors du passage
complet, puis les quatre tests de finalisation après correction de l'assertion
du modèle public canonique). Suite historique : 1 551 réussis, 45 ignorés,
dont le contrôle de colonnes historiques en lecture seule. Ruff, mypy
(89 fichiers) et les quatre contrats d'import passent. Les tests couvrent
la concurrence entre ministères, les fallbacks, le rejet sans réponse,
l'annulation, l'atomicité et la conservation des compteurs malgré la troncature.

Le smoke apparié local #241/#242 vérifie q1 MATTE et q27 MSO : quatre runs et
26 événements persistés, zéro erreur d'item/juge. Le juge valide 2/2 réponses
historiques et 1/2 réponses core ; ce panel est trop petit pour conclure.

Le panel complet utilise les 98 questions de M0a #240, leurs mêmes questions,
réponses gold et références, la configuration `51d6256b…`, le juge Scaleway
`mistral-medium-3.5-128b` en majorité de trois votes et le scope `per-question`.
Les deux moteurs utilisent le même clone pgvector 0.8.6 et écrivent leurs chats,
traces et items d'évaluation localement. Les empreintes du code, du prompt et
des questions sont attachées aux runs. Le clone a des index ANN reconstruits ;
staging était en pgvector 0.8.2. Le replay exact et la qualité live restent des
preuves distinctes.

Runs complets **locaux #243 (historique) et #244 (core)** : décision en attente
de leurs résultats. Tolérances conservées : baisse maximale de 0,05 sur
`judge_pass_rate` et `doc_recall_avg`, puis analyse par corpus. Le GO M1 n'est
pas déduit du seul smoke. Paramétrage et résultats sont consignés dans le
[journal d'expérimentations](../../evals/journal-experimentations-rag.md).
