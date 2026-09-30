# M1 — parité moteur et écritures des runs

**GO technique M1 — campagne corrigée du 30/09/2026.** Core 60/98, témoin apparié 62/98, M0a réévaluée 60/98. Seuils inchangés : baisse maximale de 5 points de pass rate et de rappel contre chaque référence.

[Résultats et intégrité](../../evals/evidence/m1_api_parity_followup_20260930.json).

Le diagnostic de la paire corrigée, en fin de document, établit des variations
du sélecteur et des incohérences du juge. Ce GO signifie le respect des seuils
préenregistrés ; il ne démontre ni une amélioration causale du code ni la
fiabilité du score absolu de qualité.

21 septembre 2026 · [#465](https://github.com/DGAFP/assistant-rh/issues/465)
· prérequis C7 : [PR #579](https://github.com/DGAFP/assistant-rh/pull/579).

**Décision du 29 septembre (historique) : NO-GO vers la phase D.** Après intégration
de `dev`, le panel apparié complet donne 62/98 PASS core contre 68/98 historique
(−6,12 points, tolérance −5 points). Les replays exacts et tests passent.
Cette décision remplace le GO du 21 septembre pour la révision courante ;
les mesures antérieures ci-dessous restent attachées à leur code.

**Suivi du 30 septembre :** départage SQL aligné et testé ; nouvelle
réévaluation complète des réponses stockées (#255–257), puis nouvelle paire
live (#258–259) avec le protocole `gold-passages-v2`, terminée. Le panel et
le snapshot sont inchangés. Le bilan complet ci-dessus donne la décision sur cette nouvelle révision.
Le [journal](../../evals/journal-experimentations-rag.md) conserve aussi
l'essai de citation littérale #252–254 interrompu pour erreurs du juge.

## Audit des écritures

L'audit initial lit les 200 derniers runs historiques et les 1 400 derniers
événements de staging, sans écrire ni copier les conversations. Il compare
leurs colonnes et taux de remplissage au run API effectué sur la copie locale
du corpus. Les nouveaux essais écrivent uniquement sur PostgreSQL local.

| Mesure | Historique observé | API avant correction | Correction |
|---|---|---|---|
| Provider et modèle de génération | 200 providers renseignés ; attribution incorrecte possible après fallback | `model` contenait le modèle public API, provider SQL vide | Provider/modèle effectivement utilisés dans les colonnes historiques ; sans étape générateur (réponse directe, échec, annulation), `model` retombe sur le modèle public demandé ; modèle public conservé dans `api_record.model` |
| Latence totale | 199/200 valeurs positives | Absente | Mesure monotone de l'exécution, avant finalisation DB |
| Durées des étapes | Colonnes `v3_*_ms` et événements | Présentes dans les événements seulement | Projection vers les colonnes historiques ; durées des retries additionnées |
| Temps au premier token | 196/200 valeurs positives ; mesuré depuis le début de génération | Non mesuré | Mesures distinctes depuis le début du run et depuis le début de génération ; en non-stream, la fin de génération vaut premier token |
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
  Sans delta (non-stream), la réponse complète est le premier token : les
  colonnes TTFT ne restent jamais `null` pour le trafic non-stream. Le pipeline
  pose lui-même les repères de génération ; `RunContext` ne connaît aucune
  étape par son nom.
- `rag_trace_events.env` reprend le label du logger historique : valeur
  normalisée (`strip`, minuscules, `production` → `prod`), vide par défaut.
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
appels LLM ; l'API laisse le provider `null` et conserve le modèle demandé
au catalogue dans ce cas.

Les agrégats historiques d'évaluation `generator_albert_est` et
`selector_albert_est` reposent sur des longueurs de texte, avec l'hypothèse d'un
provider Albert gratuit. Le pont des panels du 21 septembre n'alimentait pas
tous ces champs : leurs zéros ne mesuraient pas un usage nul. Le pont corrigé
le 28 septembre fournit ces estimations, y compris les retries du sélecteur.
Elles restent exclues du rapport de coût M1 ; les compteurs réels des appels
réussis se lisent dans les métriques des
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

## Revue finale du runner

Le pont M1 réutilise désormais `ChatService.new_context()` pour ses identifiants,
son horloge et la date française injectée dans les prompts. Il reconstruisait
auparavant une date UTC, qui pouvait différer de l'API autour de minuit.
Deux cas de régression, en hiver et en été, échouent avant correction et passent
après ; les trois tests du pont M1 et Ruff passent. Les quatre contrats d'import
ont aussi été revérifiés. Aucun fichier du runtime API ou historique ne change.
Les panels ci-dessus ont été exécutés en journée, sans changement de date entre
UTC et Paris ; leurs preuves restent rattachées au code effectivement mesuré.

## Revalidation après les corrections C7

Le correctif C7 `e8b8917`, reporté ici dans `731f075`, joint les exécutions
non-stream avant fermeture des ressources, normalise les blancs externes du
texte streamé et retire les événements d'étape inutilisés de la file SSE.
Les événements restent dans les traces. Le core métier, le stockage et le
runtime historique restent inchangés par ce correctif.

Sur la branche M1 corrigée, **979 tests passent : 976 API et trois tests du
runner**. Les sept replays M0b restent exacts ; les cinq contrôles négatifs
sont détectés. Ruff, mypy (89 fichiers) et les quatre contrats d'import passent.
Les preuves de shutdown et de normalisation figurent dans la
[validation C7](12-c7-streaming.md#corrections-après-contre-revue-du-21-septembre-2026).

Le panel live #243/#245 reste une mesure de `17c1955`, antérieure à cette
correction de transport. Aucune nouvelle campagne provider n'est lancée et
ses résultats ne sont pas réattribués au nouveau commit. La réserve MATTE et
les validations du proxy en D4/M2 restent ouvertes.

## Reprise sur dev après intégration C7 — 28 septembre 2026

La PR #580 intègre `dev` à `7a1e5ac`, comprenant C7/#579 et les corrections
des pannes DB, des sources privées, du no-answer sur contexte vide et de
l'identité de session d'audit. La résolution conserve ces comportements et
leurs tests ; le diff restant porte sur M1.

Le runner joint les deux bras, y compris les threads historiques, avant de
propager une erreur ou une annulation et de fermer ses ressources. Le délai
du core s'applique dans la boucle asyncio et attend la persistance du run
annulé. Les volumes sélecteur, retries compris, et l'estimation de longueur
de réponse alimentent désormais les champs attendus par l'évaluateur.
Ces estimations restent distinctes des compteurs d'usage réels des traces.

Validation : **1 257 tests API réussis, aucun ignoré**, sur PostgreSQL/pgvector
local jetable, y compris les 18 tests du compagnon privé M0b (7 scénarios
exacts et cinq contrôles négatifs). **1 574 tests historiques réussis,
46 ignorés**, dont les huit tests du pont M1 ; Ruff, mypy (89 fichiers) et
les quatre contrats d'import passent.

Le préflight confirme l'identité exacte des questions/golds/références avec
#240 et fige le snapshot `092e0365…` sur le code `73e5376`. Après autorisation
explicite de transmettre le panel aux providers, la campagne est exécutée
les 28–29 septembre. Les sources testées `e728708b…`, le panel `afd6cfc9…`
et le snapshot config/prompts/acronymes/corpus restent identiques jusqu'à la fin.

## Résultat complet du 29 septembre — NO-GO

Les runs initiaux #246/#247 échouent après une interruption locale et un
timeout DB ; les reprises #249/#248 s'arrêtent pendant une pression mémoire.
La dernière paire **#250 historique / #251 core termine 25/25** par bras.
L'agrégat assemble **98 IDs uniques par moteur** depuis #246/#249/#250 et
#247/#248/#251. Le core q223 de #248 est conservé mais remplacé par #251,
selon une règle fixée avant jugement, afin d'apparier la date de prompt avec
son témoin. Les 98 paires ont la même date de prompt entre moteurs. Les runs
interrompus restent en échec ; six chats sans item d'évaluation sont conservés
hors comparaison. Le journal détaille les reprises et leurs commits.

| Mesure | M0a #240 | Historique apparié | Core apparié |
|---|---:|---:|---:|
| Réponses validées | 64/98 | 68/98 | **62/98** |
| Rappel documentaire | 0,724278 | 0,729138 | **0,729138** |
| Hit rate | 0,806122 | 0,826531 | **0,826531** |

Le core respecte les tolérances contre M0a, mais échoue contre le témoin
apparié : **−0,061224 sur `judge_pass_rate`**, au-delà de −0,05. Huit
questions passent de PASS à FAIL et deux de FAIL à PASS. Les huit reculs
conservent leur rappel documentaire ; sept sont jugés incomplets. Trois
ont aussi des contextes textuellement identiques entre moteurs. Cette mesure
ne suffit pas à attribuer l'écart à une cause déterministe ou à la variance.
Hors des huit questions déjà taguées instables, historique 63/90 et core 59/90 ;
ce diagnostic ne remplace pas le seuil fixé sur les 98 questions.

Les 98 jugements de chaque bras sont terminés ; un jugement core repose sur
deux votes concordants sur trois demandés. Son usage juge est incomplet et
le coût total reste inconnu. Les 290 appels LLM core réussis ont leur usage ;
une tentative de classification échouée reste sans compteur factice.

Relecture des 196 chats évalués : **98 chats, 578 événements historiques** ;
**98 chats, 676 événements et 203 sources core**, sans incohérence détectée
de trace, scope, métriques ou persistance. Aucun chevauchement inter-ministères
n'a été observé dans ce panel ; la preuve d'isolation concurrente reste celle
des tests déterministes. Les limites du clone pgvector 0.8.6, des index
reconstruits et du coût incomplet restent explicites.

**NO-GO M1 : la phase D reste bloquée et #580 reste en brouillon.** La dette
de parité reportée au LEDGER est vide et les tests techniques passent, mais
le seuil de qualité live n'est pas satisfait. Les cas discordants sont
documentés sans ajustement du seuil ni relance sélective selon les scores.
[Preuve agrégée](../../evals/evidence/m1_api_parity_local_20260928.json) et
[journal complet](../../evals/journal-experimentations-rag.md). Aucun déploiement.

## Diagnostic du 30 septembre 2026

L'examen des 196 items et la reconstruction des prompts distinguent plusieurs
mécanismes. **Le NO-GO et les scores 62/98 contre 68/98 restent inchangés.**
L'enquête est en lecture seule sur le clone, sans nouvel appel provider.

Sur les 96 paires passant par le générateur, les prompts système reconstruits
sont identiques. Les messages utilisateur le sont dans **56 paires**, dont
**54 produisent pourtant des réponses différentes**. Les prompts du sélecteur
sont identiques dans **64 paires**, dont **26 produisent des sélections
différentes**. Les 192 réponses enregistrées du sélecteur donnent le même
résultat avec les deux parseurs. Les deux autres paires sont des réponses
directes, sans génération. Ces constats localisent une variation des sorties
LLM ; ils ne mesurent pas une probabilité de régression propre à un moteur.

| Cas en recul | Ce que les traces établissent |
|---|---|
| q6 — rémunération | Même prompt sélecteur ; le core écarte la fiche MATTE n°3, conservée par l'historique. La réponse perd les règles ministérielles de fixation de la rémunération. |
| q20 — temps partiel | Même prompt sélecteur ; le core retire le texte complémentaire sur le temps partiel de droit. La réponse traite le temps partiel sur autorisation. |
| q28 — démission MSO | Même prompt sélecteur ; le core ne conserve que la section ministérielle et retire la fiche Service-Public sur les conséquences de la démission. Les conséquences sur les congés ne sont plus expliquées. |
| q4538 — déontologie | Même prompt sélecteur ; le core ajoute un article sur le recrutement devant la même fiche MATTE. La réponse traite les activités antérieures au recrutement, au lieu du projet d'activité privée au départ. Le label juge `retrieval_gap` ne localise pas correctement cette erreur. |
| q188 — contrat de projet | Le prompt sélecteur diffère déjà : des sections Service-Public ambiguës sont résolues différemment. La fiche MATTE pertinente reste toutefois présente à l'indice 14 dans les deux bras. L'historique la choisit ; le core choisit l'article à l'indice 0 et omet la restriction relative au CDI. Le lien causal entre l'ambiguïté SQL et ce choix LLM n'est pas isolé. |
| q186 — renouvellement | Mêmes messages de génération. Les deux réponses omettent la réserve sur les motifs illégaux. Le juge historique lui attribue pourtant cette précision, puis le juge core reproche son absence. |
| q827 — arrêt maladie | Mêmes messages de génération. Les deux réponses omettent la sanction d'envoi tardif, que le juge historique affirme présente. Le juge core déclare aussi absentes la transmission des volets à la CPAM et l'information de reprise anticipée, alors qu'elles figurent dans la réponse. |
| q926 — temps partiel thérapeutique | Mêmes messages de génération. L'accord d'indemnisation CPAM n'est explicité dans aucune des deux réponses. Le juge historique l'affirme couvert ; le juge core pénalise son absence. |

Les trois derniers cas montrent une application incohérente de la grille du
juge, y compris avec trois votes concordants côté historique. Cela ne certifie
pas les réponses core : certaines omissions sont communes aux deux bras.
Il faut confronter les motifs du juge aux phrases effectivement présentes,
et non interpréter un PASS comme une annotation humaine fiable. Cet audit
ciblé ne remplace pas une réévaluation du panel selon un protocole fixé avant
exécution. Aucun vote ni statut enregistré n'est modifié.

**Pourquoi le rappel global n'a-t-il pas baissé ?** `retrieved_doc_ids()`
regroupe les références du contexte final **et** celles des chunks récupérés
et rerankés. Une source retrouvée puis supprimée par le sélecteur reste donc
comptée. Pour q6, le rappel global vaut 1 dans les deux bras, mais celui du
contexte effectivement servi passe de **1 à 0,6**. Sur les 96 paires RAG,
le rappel moyen à la sortie du context builder passe de **0,460268 à 0,450397**.
L'égalité de la métrique globale ne prouve pas l'égalité des contextes.

L'écart de résolution SQL est reproduit avec les deux helpers réels : le
runtime historique trie les sections par correspondance exacte de chemin,
puis applique `LIMIT 1` sans départager les autres égalités ; le core ajoute
`section_id`. Un même titre peut désigner plusieurs sections de la même fiche,
avec des textes différents. Des associations chunk/section différentes sont
observées dans 74 des 96 paires RAG, sans nécessairement changer le contexte
final. Il s'agit de la limite B2 déjà documentée : les replays M0b injectent
les mêmes entrées aux ports et ne prouvent pas l'équivalence de ces SQL
ambigus. Le test différentiel DB retire d'ailleurs le doublon de sa fixture.
Cette limite ne justifie pas de supprimer le tri déterministe du core.

Méthode : reconstruction avec les fonctions de formatage versionnées et le
snapshot inchangé `092e0365…`. Les prompts utilisateur historiques complets,
les préfixes système historiques, les traces core après masquage/troncature
et les longueurs intégrales enregistrées concordent. Les messages core
complets n'étaient pas conservés : leur égalité est **reconstruite et
recoupée**, pas issue d'une capture HTTP intégrale. Aucune preuve n'attribue
les huit reculs à un unique défaut du moteur.

Suite recommandée : isoler sélecteur et génération sur des entrées communes
figées ; contrôler le juge par des citations vérifiables dans les deux bras ;
traiter séparément la résolution des sections ambiguës avec une politique
commune ou un mapping de données explicite. Toute nouvelle mesure conserve
le panel et les seuils fixés avant lancement.

[Preuve du diagnostic](../../evals/evidence/m1_api_parity_diagnosis_20260930.json)
`0bdc551f3459157dae63b95db68863d73b177b0e8a580fc696be5f4bcb90eba7`.
Les prompts, réponses et contextes détaillés restent privés dans
`/tmp/assistant-rh-m1-investigate-20260930/`.

## Diagnostic de la paire corrigée #258–259 — 30 septembre 2026

**60/98 core, 62/98 historique : six pertes et quatre gains**, soit −2,04 points.
Le rappel global est identique (0,729138). Les seuils contre le témoin apparié
et M0a réévaluée (60/98) sont respectés. Le rejeu du juge sur les anciennes
réponses donne toujours 58/98 contre 64/98 : les résultats historiques restent
conservés. Aucun vote n'a été modifié par cette analyse.

| Vérification sur la paire corrigée | Résultat |
|---|---:|
| Questions / parcours RAG / réponses directes | 98 / 96 / 2 |
| Associations chunk→section divergentes parmi les chunks communs | 0 sur 12 506 observations |
| Ordre des chunks récupérés identique | 94/96 parcours RAG |
| Prompts du sélecteur identiques | 95/96 |
| Sélections effectivement servies différentes avec un même prompt | 49/95 |
| Paires de messages système + utilisateur identiques au générateur | 51/96 |
| Réponses différentes parmi ces paires de messages identiques | 50/51 |
| Sorties brutes du sélecteur interprétées pareil par les deux parseurs | 192/192 |

Le départage SQL est commun sur tout le panel observé. Les deux moteurs
configurent le sélecteur Albert `openweight-large` et le générateur
`deepseek-v4-flash` à température 0. Leurs sorties varient néanmoins ;
ces observations n'identifient pas le mécanisme interne chez le fournisseur.

Huit réponses du sélecteur échouent au parsing : cinq côté historique
(q1, q6, q182, q199, q210), trois côté core (q210, q827, q4528). Les deux
parseurs réagissent pareil à chacune ; le repli conserve les cinq premiers
candidats. Sur q827, la clôture du bloc JSON manque. C'est une fragilité
commune, susceptible de changer le contexte, pas une divergence de parsing.

q1 est la seule requête de recherche différente : le classificateur formule
différemment les acronymes. q203 échange deux chunks aux positions 95 et 98,
sans changer leur ensemble ni le prompt sélecteur ; la cause de ce classement
n'est pas isolée. Le seul fallback de génération core est q222 vers Scaleway ;
les deux réponses échouent, donc il ne contribue pas aux verdicts discordants.

Les dix cas suivants sont des observations diagnostiques, parfois mixtes ;
ils ne remplacent pas les verdicts officiels par de nouvelles annotations.

| Cas | Core vs historique | Constat |
|---|---|---|
| q1 | Gain | Requête et contexte différents ; repli de parsing historique. L'interprétation du juge sur la poursuite du contrat mérite aussi une revue. Ce cas n'isole pas un effet du moteur. |
| q6 | Perte | Même prompt sélecteur. Le repli historique conserve la fiche ministérielle ; le core l'écarte. Rappel du contexte servi : 1 → 0,6 ; détails ministériels absents de la réponse core. |
| q15 | Gain | Le core ajoute une section de procédure et répond plus directement. Le juge lui attribue toutefois une prestation absente de la réponse et tolère une condition qu'il critique côté historique. |
| q177 | Gain | Mêmes messages de génération. Les deux réponses omettent le même délai de renouvellement. Complétude historique : 0,667 aux trois votes ; core : 0,8 / 1 / 0,6. Un vote core déduit le délai d'une citation qui ne le contient pas. |
| q186 | Perte | La même réserve sur les motifs illégaux manque dans les deux réponses. Le juge la crédite implicitement côté historique et pénalise son absence côté core, à chaque vote. |
| q189 | Gain | Mêmes messages de génération. Les deux réponses signalent une absence d'information dans la source ; le juge échoue l'historique pour abstention mais transforme celle du core en affirmation d'absence de droit. |
| q224 | Perte | Même prompt sélecteur ; historique : indices 0 et 2, core : 9. Le rappel du contexte servi tombe de 1 à 0 et le core s'abstient. L'information utile était disponible avant filtrage. |
| q225 | Perte | Même prompt sélecteur ; le core remplace la fiche ministérielle à l'indice 2 par Service-Public à l'indice 3. La règle relative aux grilles internes disparaît de la réponse. |
| q827 | Perte | Repli de parsing core et contextes différents. Les deux réponses omettent deux exigences du gold, avec des pénalités très différentes. Un vote core déclare absente une précision temporelle présente dans sa propre citation. |
| q4530 | Perte | Le core retire une section complémentaire. La table principale reste disponible dans les deux bras, mais la formulation core insiste sur un document en amont et reçoit un verdict de contradiction. Revue du critère nécessaire. |

q177, q186, q189 et q827 établissent des incohérences de notation dans les deux
directions. Trois votes concordants ne garantissent pas une lecture juste.
`gold-passages-v2` assure la provenance des citations, pas qu'elles démontrent
le point crédité. Chaque vote choisit encore les points requis, leur découpage
et sa note de complétude. Le même passage gold peut ainsi être compté
différemment selon le candidat.

Le rappel global inclut les références récupérées avant filtrage : il reste
à 1 dans les deux bras de q224 alors que le rappel du contexte servi core est
nul. Il ne permet donc pas de disculper le sélecteur.

Suite prioritaire :

1. Fixer les critères atomiques et leur poids **une fois par question**, pour
   les deux candidats. Calculer la complétude sur cette liste et revoir les
   citations sur les quatre cas les plus nets.
2. Injecter les **mêmes sorties de modèles enregistrées** dans les deux moteurs
   pour isoler les différences de code. Les sept scénarios M0b existants ne
   constituent pas un rejeu des 98 questions de cette campagne.
3. Traiter les enveloppes mal formées du sélecteur de façon commune, puis
   rejouer hors ligne les huit échecs. Suivre séparément la couverture du
   contexte servi et les sources complémentaires supprimées.

Une nouvelle campagne en direct ne résoudrait pas seule ces problèmes de
mesure. Le passage de −6 à −2 réponses ne peut pas être attribué au seul
correctif SQL : les deux bras ont produit de nouvelles réponses.

Méthode : prompts reconstruits sur le snapshot inchangé, recoupés avec les
messages utilisateur historiques complets, les préfixes système, les traces
core masquées/tronquées et les longueurs intégrales. Ce n'est ni une capture
HTTP intégrale ni une validation juridique du goldset. Aucun nouvel appel
fournisseur pour ce diagnostic ; textes détaillés privés.

Les 196 items ont été persistés avant disparition du processus pendant la
finalisation. Les agrégats reconstruits par projection bornée sont identiques
à ceux déjà finalisés côté historique ; le statut et les agrégats core ont
été finalisés depuis les lignes existantes, sans rejouer d'item. Cause de
terminaison non établie ; l'export massif reste à fiabiliser.

[Preuve agrégée](../../evals/evidence/m1_api_parity_corrected_diagnosis_20260930.json)
`cfcb0925ec9d3d43f1aeb6133a955f542baed2e838155815835277717ed458aa`.
