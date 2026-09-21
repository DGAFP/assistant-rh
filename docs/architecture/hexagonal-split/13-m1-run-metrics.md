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
Le logger historique peut aussi garder le provider/modèle configuré lors d'un
court-circuit sans génération. Ces colonnes seules ne comptent donc pas les
appels LLM ; l'API les laisse `null` dans ce cas.

Les agrégats historiques d'évaluation `generator_albert_est` et
`selector_albert_est` reposent sur des longueurs de texte, avec l'hypothèse d'un
provider Albert gratuit. Le pont M1 core n'alimente pas tous ces champs : leurs
zéros ne mesurent pas un usage nul. Ils sont exclus du rapport de comparaison
M1 ; les compteurs réels des appels réussis se lisent dans les métriques des
traces, séparément de l'usage du juge. La consolidation du coût complet reste
à faire avec D4/M2, notamment pour les appels interrompus et les fallbacks.

## Preuves et protocole M1

Le premier panel mesure `16e6afe`. L'inspection du cas q4 révèle un écart
déterministe : `freeze_json` triait les clés des références Service-Public
retournées par PostgreSQL, puis le contexte rendait ces objets en chaînes.
Les mêmes documents produisaient ainsi un texte de prompt différent.
Le correctif `17c1955` conserve l'ordre des données, tout en gardant le tri
canonique dans le calcul des révisions. Le nouveau cas SQL synthétique
`service-public-references` échoue avant correction et passe après. Il complète
le replay aux ports, qui n'exerçait pas cette transformation de l'adaptateur DB.

Les sept scénarios du compagnon M0b restent exacts après correction,
sur 27 sorties d'étapes et sept résultats finaux ; cinq mutations négatives
sont détectées. La suite API complète valide **964 tests** sur le correctif.
Suite historique pour les corrections de métriques : 1 551 réussis, 45 ignorés,
dont le contrôle de colonnes historiques en lecture seule. Ruff, mypy
(89 fichiers) et les quatre contrats d'import passent. Les tests couvrent
la concurrence entre ministères, les fallbacks, le rejet sans réponse,
l'annulation, l'atomicité et la conservation des compteurs malgré la troncature.

Le smoke apparié local #241/#242 vérifie q1 MATTE et q27 MSO : quatre runs et
26 événements persistés, zéro erreur d'item/juge. Le juge valide 2/2 réponses
historiques et 1/2 réponses core ; ce panel est trop petit pour conclure.
Un tour streamé supplémentaire sur le correctif valide 87 deltas, un run et
sept événements relus : TTFT du run 6 063 ms, TTFT génération/SQL 298 ms,
usage Albert déclaré 2 545 tokens d'entrée et 330 de sortie. Il exerce le
service streamé et la persistance ; les preuves de transport restent C7.

Le panel complet utilise les 98 questions de M0a #240, leurs mêmes questions,
réponses gold et références, la configuration `51d6256b…`, le juge Scaleway
`mistral-medium-3.5-128b` en majorité de trois votes et le scope `per-question`.
Les deux moteurs utilisent le même clone pgvector 0.8.6 et écrivent leurs chats,
traces et items d'évaluation localement. Les empreintes du code, du prompt et
des questions sont attachées aux runs. Le clone a des index ANN reconstruits ;
staging était en pgvector 0.8.2. Le replay exact et la qualité live restent des
preuves distinctes.

Les trois runs **locaux #243 (historique), #244 (core initial) et #245 (core
corrigé)** sont terminés, 98/98 chacun et zéro erreur d'item/juge. #245 réutilise
le témoin #243, sans recalculer ses générations ou ses votes juge.

| Mesure | M0a #240 | Historique #243 | Core corrigé #245 |
|---|---:|---:|---:|
| Réponses validées | 64/98 | 64/98 | **67/98** |
| Rappel documentaire | 0,7243 | 0,7291 | **0,7291** |
| Hit rate | 0,8061 | 0,8265 | **0,8265** |

Les baisses maximales autorisées de 0,05 sur `judge_pass_rate` et
`doc_recall_avg` sont respectées contre les deux références. Hors des huit
questions déjà taguées `juge_borderline`, les deux bras locaux sont à 62/90 :
le gain global ne prouve pas une amélioration due au correctif. Le sous-panel
MATTE recule de 7 à 5 PASS sur 12 (q4 et q33), avec le même rappel documentaire ;
ces cas restent suivis en canary. Le détail par corpus figure au journal.

La relecture finale rapproche 98 items core avec **98 chats, 676 événements et
206 sources**, sans incohérence. Les 290 appels LLM réussis ont des compteurs
d'usage ; trois tentatives de classification échouées avant reprise restent
sans usage factice. Deux requêtes MSO/MATTE se chevauchent réellement. Les
comptages du corpus et les empreintes du code/questions n'ont pas changé.

**GO technique M1 vers D1–D4 après intégration de #579/#580.** La dette de
parité du LEDGER est vide ; les fallbacks, no-answer, concurrence et atomicité
sont couverts. Le déploiement dark, le proxy et l'opérabilité restent D4/M2.
Code mesuré `17c1955`, empreinte sources `5ddfa118…`, preuve `96809b97…` :
[rapport agrégé](../../evals/evidence/m1_api_parity_local_20260921.json) et
[journal d'expérimentations](../../evals/journal-experimentations-rag.md).
