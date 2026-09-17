# Définitions des mesures du dashboard Évaluations RAG

Ces définitions expliquent les métriques enregistrées, sans changer le scoring ou rejouer les runs. Les versions historiques restent à interpréter selon leur protocole.

## Dimensions du juge et RAGAS

### Score global du juge

Moyenne des scores enregistrés par question, après la calibration et les plafonds prévus par le protocole. Ce n’est ni le taux de PASS, ni une probabilité que la réponse soit correcte. Un plafond de diagnostic retrieval peut abaisser ce score sans annuler un PASS. En protocole multi-votes, ne pas le lire comme la moyenne de tous les votes.

### Exactitude juridique

Le juge vérifie les règles de droit, obligations, exceptions et nuances de la réponse. Une règle erronée ou une nuance trompeuse baisse la note ; une réponse concise mais juridiquement correcte peut obtenir une note élevée.

### Complétude

Les éléments nécessaires pour répondre et agir sont-ils présents : conditions, délais, exceptions et points obligatoires de la réponse de référence ? La longueur, les exemples et les détails facultatifs ne sont pas récompensés. Une réponse courte peut être complète.

### Accord avec la réponse de référence

La conclusion est-elle cohérente avec la réponse gold ? Une contradiction ou une conclusion juridiquement plus forte ou plus faible réduit la note. Ce n’est pas une similarité mot à mot : reformuler ou ajouter une information correcte n’est pas un défaut en soi.

### Appui sur les sources

Les affirmations sont-elles étayées par la réponse de référence ou par les contextes fournis au juge ? Ce jugement sur le contenu ne vérifie pas la présence d’un identifiant gold dans le retrieval ; il peut donc être élevé alors que le hit documentaire est faible.

### RAGAS · fidélité

Part des affirmations de la réponse générée que RAGAS peut justifier à partir des contextes fournis. Une valeur élevée indique un bon ancrage dans ces contextes, sans prouver leur exactitude juridique. Moyenne des scores disponibles ; absent si RAGAS n’a pas produit de mesure.

### RAGAS · précision du contexte

Les extraits utiles pour répondre à la référence sont-ils placés en tête des contextes ? Cette précision tient compte du classement (average precision). Ce n’est pas la proportion d’identifiants gold retrouvés, ni la simple précision documentaire. Moyenne des scores disponibles.

### RAGAS · rappel du contexte

Part des éléments de la réponse de référence que les contextes fournis permettent de justifier, selon RAGAS. Ce rappel porte sur l’information exprimée, pas sur les identifiants des documents gold. Moyenne des scores disponibles ; une mesure absente n’est pas remplacée par zéro.

## Étapes de la tentative

### Pool avant reranking

Documents identifiés dans les chunks candidats avant le reranking des sections, pour la tentative choisie. Cette étape indique si la recherche a trouvé le gold dans son ensemble de candidats ; elle ne garantit pas qu’il sera conservé ensuite.

### Sections top-20

Documents représentés dans les 20 premières sections agrégées après reranking, dans l’ordre enregistré. k compte des sections, pas des documents ni des chunks : un même document peut occuper plusieurs places. S’il reste moins de 20 sections, toutes sont prises en compte.

### Top-12 · diagnostic

Même liste de sections classées, limitée aux 12 premières. C’est une coupe alternative pour mesurer l’effet d’un k plus petit, pas une étape exécutée entre top-20 et sélection. La différence top-20 / top-12 indique ce que la coupe retire, sur des questions mesurées comparables.

### Sélection conservée

Documents des sections que le sélecteur a explicitement gardées, selon les décisions enregistrées. Une trace absente n’est pas une sélection vide : elle est marquée Non tracé et exclue des moyennes correspondantes.

### Contexte construit

Documents référencés dans le contexte produit par ContextBuilder pour la tentative choisie, après construction et budget de contexte. Si un retry remplace cette tentative, cette mesure initiale ne décrit pas à elle seule le contexte finalement envoyé au générateur.

## Retrieval historique

### Présence d’une source attendue · hit

Pour une question : 1 si au moins un identifiant gold est retrouvé, sinon 0. Le taux est le nombre de questions avec un hit divisé par le nombre de questions mesurées. Exemple : retrouver 1 source attendue sur 3 donne un hit de 100 %, mais un recall de 33,3 %. Dans le diagnostic historique, la recherche porte sur l’union de plusieurs étapes, pas uniquement sur le contexte construit.

### Rappel documentaire · recall

Pour chaque question : nombre d’identifiants gold retrouvés / nombre d’identifiants gold attendus, selon le matching enregistré (aliases inclus). On moyenne ensuite les ratios des questions mesurées, sans pondérer par leur nombre de sources. Le diagnostic historique utilise l’union des étapes ; le funnel calcule ce rappel séparément à chaque étape.

### Précision documentaire historique

Pour chaque question : identifiants gold retrouvés / identifiants retrouvés dans la liste historique fusionnée, puis moyenne des ratios disponibles. Des sources pertinentes non présentes dans le gold peuvent faire baisser cette précision ; une faible valeur ne prouve donc pas que tous les autres documents sont inutiles. Matching historique avec aliases.

### Rang réciproque moyen · MRR

Pour une question : 1 / rang du premier identifiant gold dans la liste historique, ou 0 si aucun n’est retrouvé, puis moyenne entre questions. Au rang 1 : 100 % ; au rang 3 : 33,3 %. Cette liste fusionne des références de plusieurs étapes : ce n’est pas le rang du seul reranker ni du seul contexte final.

## Colonnes et indicateurs

### Questions avec gold

Numérateur du hit : nombre de questions ayant au moins un identifiant gold retrouvé à cette étape.

### Questions mesurées (hit)

Dénominateur du hit : questions avec un hit valide (0 ou 1) à cette étape et cette tentative. Traces absentes, absence de gold explicite et hits invalides sont exclus.

### Gold retrouvé (%)

Hit d’une question = 1 si au moins une source gold est retrouvée, sinon 0. Taux de hit = questions avec gold / questions dont le hit est mesuré. Retrouvé ne signifie pas que toutes les sources sont présentes, ni que la réponse générée est correcte.

### Recall moyen

Rappel d’une question = identifiants gold retrouvés / identifiants gold attendus, avec le matching historique et ses aliases. Rappel moyen = moyenne simple des ratios valides des questions mesurées à cette étape ; ce n’est pas le ratio des totaux de documents. Un tiers retrouvé vaut 33,3 %, même si le hit vaut 100 %.

### Questions mesurées (recall)

Dénominateur du rappel moyen : questions avec un ratio de recall valide à cette étape. Peut différer du dénominateur du hit.

### Sans trace

Nombre d’items sans diagnostic de cette étape pour cette tentative. Une tentative non exécutée peut aussi être sans trace.

### Sans gold

Nombre d’items dont le diagnostic indique explicitement zéro identifiant gold attendu. Aucun rappel documentaire n’est calculable pour ces items.

### Hit invalide

Nombre d’items dont l’étape existe mais dont le hit n’est pas une mesure binaire exploitable. Ce n’est pas un hit à zéro.

### Étape

Emplacement de la mesure dans la tentative choisie : pool, sections top-20, top-12 diagnostique, sélection et contexte construit. k désigne un nombre de sections.

### Transition

Deux étapes mises en regard sur les mêmes questions mesurées. Top-12, coupe alternative, n’est pas intercalé dans ce parcours.

### Questions appariées

Questions avec un hit valide avant ET après la transition, y compris celles où aucun gold attendu n’a été retrouvé aux deux étapes (hit 0 → 0). Les questions sans gold attendu explicite sont exclues.

### Gold conservé

Questions appariées avec au moins un gold avant et après : hit 1 → 1. Cela peut masquer une perte partielle de recall.

### Gold perdu (oui → non)

Questions appariées qui avaient au moins un gold avant et n’en ont plus après : hit 1 → 0.

### Gold retrouvé (non → oui)

Questions appariées sans gold avant mais avec au moins un gold après : hit 0 → 1.

### Question ID

Identifiant de la question dans le goldset. Les jointures utilisent en interne l’ID de l’item pour ne pas confondre deux évaluations de la même question.

### Question

Intitulé enregistré lors de l’évaluation, éventuellement abrégé. La formulation actuelle dans le goldset peut avoir changé depuis.

### Sources gold attendues

Libellés de référence enregistrés avec la question. Les métriques utilisent le matching d’identifiants historique, aliases inclus ; cette colonne n’est pas une liste des sources effectivement conservées.

### Verdict juge

Verdict enregistré pour cette question : PASS ou FAIL selon le protocole du run (calibration, seuils, vote majoritaire éventuel). Non disponible = jugement absent ou inexploitable. Ce verdict ne se déduit ni du seul score affiché ni de la présence du gold.

### Score juge

Score enregistré pour cette question, après calibration et plafonds du protocole. Ce n’est ni une moyenne du run ni une probabilité de réponse correcte. Un plafond de diagnostic retrieval peut abaisser ce score sans annuler un PASS. En multi-votes, ce score n’est pas nécessairement la moyenne des votes.

### Recall contexte

Rappel d’une question = identifiants gold retrouvés / identifiants gold attendus, avec le matching historique et ses aliases. Rappel moyen = moyenne simple des ratios valides des questions mesurées à cette étape ; ce n’est pas le ratio des totaux de documents. Un tiers retrouvé vaut 33,3 %, même si le hit vaut 100 %. Étape : contexte construit de la tentative choisie.

### Run

Identifiant de l’exécution enregistrée ; utilisez le filtre Run pour afficher son détail.

### Environnement

Base et environnement dont proviennent les évaluations enregistrées. Les runs de staging et de production ne sont pas mélangés.

### Goldset

Jeu de questions nommé utilisé pour le run ; un même nom peut évoluer au fil des versions.

### Limite

Limite de questions prévue par le protocole. 0 = aucune limite explicite ; None/unknown = valeur non précisée. Cela ne prouve pas la complétude du goldset.

### Juge

Modèle d’évaluation enregistré, distinct du modèle qui a généré la réponse.

### Commit

Révision de code enregistrée pour ce run ; utile pour retrouver le protocole et le pipeline utilisés.

### Configuration

Empreinte de la configuration enregistrée. Identique ne prouve pas que les données et sources sont identiques.

### Périmètre

Empreinte des paramètres de sélection et tags du run, pas une preuve d’appariement des questions ou du corpus.

### Créé le

Date de création du run, affichée dans le fuseau du dashboard ; indépendante de la date de collecte des métriques.

### Pool

Documents identifiés dans les chunks candidats avant le reranking des sections, pour la tentative choisie. Cette étape indique si la recherche a trouvé le gold dans son ensemble de candidats ; elle ne garantit pas qu’il sera conservé ensuite. Retrouvé : au moins une source gold ; Absent : mesure valide sans gold retrouvé ; Non tracé : étape absente de l’enregistrement ; Sans gold : aucun gold à mesurer ; Mesure invalide : hit inexploitable. Les trois derniers cas ne deviennent pas des échecs de retrieval.

### Top-20

Documents représentés dans les 20 premières sections agrégées après reranking, dans l’ordre enregistré. k compte des sections, pas des documents ni des chunks : un même document peut occuper plusieurs places. S’il reste moins de 20 sections, toutes sont prises en compte. Retrouvé : au moins une source gold ; Absent : mesure valide sans gold retrouvé ; Non tracé : étape absente de l’enregistrement ; Sans gold : aucun gold à mesurer ; Mesure invalide : hit inexploitable. Les trois derniers cas ne deviennent pas des échecs de retrieval.

### Top-12*

Même liste de sections classées, limitée aux 12 premières. C’est une coupe alternative pour mesurer l’effet d’un k plus petit, pas une étape exécutée entre top-20 et sélection. La différence top-20 / top-12 indique ce que la coupe retire, sur des questions mesurées comparables. Retrouvé : au moins une source gold ; Absent : mesure valide sans gold retrouvé ; Non tracé : étape absente de l’enregistrement ; Sans gold : aucun gold à mesurer ; Mesure invalide : hit inexploitable. Les trois derniers cas ne deviennent pas des échecs de retrieval.

### Sélection

Documents des sections que le sélecteur a explicitement gardées, selon les décisions enregistrées. Une trace absente n’est pas une sélection vide : elle est marquée Non tracé et exclue des moyennes correspondantes. Retrouvé : au moins une source gold ; Absent : mesure valide sans gold retrouvé ; Non tracé : étape absente de l’enregistrement ; Sans gold : aucun gold à mesurer ; Mesure invalide : hit inexploitable. Les trois derniers cas ne deviennent pas des échecs de retrieval.

### Contexte

Documents référencés dans le contexte produit par ContextBuilder pour la tentative choisie, après construction et budget de contexte. Si un retry remplace cette tentative, cette mesure initiale ne décrit pas à elle seule le contexte finalement envoyé au générateur. Retrouvé : au moins une source gold ; Absent : mesure valide sans gold retrouvé ; Non tracé : étape absente de l’enregistrement ; Sans gold : aucun gold à mesurer ; Mesure invalide : hit inexploitable. Les trois derniers cas ne deviennent pas des échecs de retrieval.

## Sources des définitions

- Implémentation du juge, agrégats et funnel : `src/goldset/eval.py` (`aggregate_items`, `calibrate_judge_result`, `judge_answer`, `stage_retrieval_metrics`).
- Collecte des dénominateurs et transitions : `packages/data-engineering/src/assistant_rh_data_engineering/jobs/rag_eval_items.py`.
- [Métriques RAGAS](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/).
- [Aides des panneaux Grafana](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/configure-panel-options/).
