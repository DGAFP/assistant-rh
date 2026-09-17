# M0b — compagnon du moteur complet, 17 septembre 2026

**7 scénarios sur 7 égaux, étape par étape et sur le résultat métier final**, en
rejouant les entrées enregistrées dans la composition de production de la PR
[#560](https://github.com/DGAFP/assistant-rh/pull/560). Les cinq contrôles négatifs
échouent comme attendu. Ce compagnon apporte la preuve d'assemblage C6 sur le
panel M0b, suivant le protocole de complément prévu dans
[M0_REPLAYS.md](../../M0_REPLAYS.md).

Il s'agit d'un nouvel enregistrement du runtime hérité retenu et des ports,
pas d'une récupération des appels du 1er septembre. Le bundle historique
`m0-api-parity-dev-9bf1cf0` reste inchangé : 7 fixtures, 56 artefacts,
empreinte `f5e9ffefe588248a352d7ac18a556df7bff6270a2879e7ddd32acc128733e02b`.
Son auto-vérification garde `exact_comparison: null` ; le **nouveau** rapport
[comparison.json](comparison.json) porte `exact_comparison: true`.

## Enregistrement et provenance

L'accès à staging et les appels réels Albert/DINUM, avec secours Scaleway
configuré, ont été autorisés. Toutes les lectures partagent un snapshot
PostgreSQL `REPEATABLE READ`, en lecture seule, sur 4 882 documents et 10 397
sections. Aucun run/session ni fixture n'est écrit sur staging. La capture se
termine le 17 septembre 2026 à 13:38:24 UTC ; identité du snapshot et version
PostgreSQL figurent dans le manifeste. Le snapshot distant est ensuite fermé :
les réponses de ports archivées constituent la référence portable.

La base du checkout est `395ad028d6009f6e31e5f1760e795868be56ffb7`.
**`source_commit` désigne cette base, pas un arbre Git propre** : la capture
inclut la correction SQL et l'instrumentation non encore commitées. Les 114
empreintes `source_hashes` et les fichiers correspondants dans
`recording-source/` identifient exactement les sources utilisées. Les tests
vérifient ces empreintes. Le rapport de replay identifie séparément les sources
API du checkout exécuté. Les validations explicites du runner publié ont été
durcies après capture pour rester actives sous `-O` et `-OO` ; les entrées et
sorties enregistrées ne sont pas réécrites.

L'archive contient seulement le manifeste, les sept cas et les sources de
l'enregistreur/runtime. Les logs privés, `.env`, DSN et clés ne sont pas publiés.
Les réponses incluent les extraits documentaires nécessaires à la conformance.

## Ce qui est comparé

Le runtime hérité exécute son vrai pipeline. L'instrumentation observe les
réponses LLM avant parsing, le vecteur d'embedding, les réponses reranker,
les lignes SQL avant scoring des titres/fusion RRF, et les lectures de contenu
avant agrégation. Pour les recherches hybrides, les CTE du SQL hérité sont
exécutés sur le même snapshot pour enregistrer les deux voies avant leur
fusion et la limite finale. SQL, paramètres et lignes brutes sont conservés.
Les résultats attendus viennent séparément des retours des étapes héritées.
Aucune entrée de recherche n'est reconstruite depuis les chunks attendus.

Le replay appelle `bootstrap.create_chat_service`, puis le vrai `ChatService`
et les six étapes de `Pipeline`. Seuls les ports externes sont remplacés par
leurs réponses enregistrées ; horloge, identifiants, authentification et
finalisation du run sont locaux. Chaque appel doit correspondre à ses arguments
complets et consommer une réponse. Un appel manquant, ajouté ou répété échoue.
Les lots d'identifiants des lectures de sections/références sont comparés comme
ensembles ; l'ordre des **réponses** reste strictement celui enregistré.
Les sockets réseau sont bloquées pendant le replay.

La comparaison exacte conserve types JSON, tableaux ordonnés, scores sans
arrondi, métadonnées complètes, décisions, contexte et octets des prompts.
Elle utilise les résultats complets des étapes, pas les traces opérationnelles
tronquées. Le résultat métier comprend réponse, contexte, sources héritées et
indicateurs selector. Le rendu public C1, ses sources documentaires dédupliquées,
ses URL filtrées, l'authentification HTTP et la persistance PostgreSQL sont
validés séparément par les tests API ; ils ne sont pas annoncés identiques au
format hérité.

| Scénario | Étapes exécutées | Appels inference | Sources métier | Résultat |
|---|---:|---:|---:|---|
| rag-acronym-contract | 6 | 5 | 1 | exact |
| rag-legal-dgafp | 6 | 5 | 1 | exact |
| rag-conversation-followup | 6 | 5 | 3 | exact |
| rag-ministry-mso | 6 | 5 | 2 | exact |
| short-circuit-chit-chat | 1 | 1 | 0 | exact |
| short-circuit-document-request | 1 | 1 | 0 | exact |
| short-circuit-out-of-scope | 1 | 1 | 0 | exact |

Les [contrôles négatifs](negative-controls.json) altèrent une fixture sans
mettre à jour son hash, une requête provider, un rang brut hybride, une réponse
attendue et l'inventaire des appels de ports. Pour les quatre derniers, les
hashes sont recalculés afin de vérifier le comportement, pas seulement
l'intégrité du fichier. Les tests CI exécutent le replay sur le **checkout
courant**, protègent l'inventaire des sept cas et vérifient aussi les commandes
avec optimisation Python.

## Régression SQL et limites

La comparaison exploratoire des adaptateurs réels a trouvé 134 candidats API
contre 133 hérités. Un `ROW_NUMBER` placé avant `LIMIT` changeait le plan vers
un index IVFFlat approximatif pour la recherche sémantique seule. Le correctif
numérote désormais les candidats après la requête de sélection héritée ; le
classement des voies hybrides est conservé. Les candidats et scores de cette
sonde deviennent identiques (133). Le test
`test_indexed_search_conformance.py` reproduit la divergence avec 2 500 vecteurs
synthétiques de dimension 1 024 et un index IVFFlat : échec avant le correctif,
succès après. Son coût de pages ajusté reste local au test.

La même sonde révèle encore 13 associations de sections Service-Public
différentes : B2 départage les correspondances ambiguës par UUID, tandis que le
SQL hérité utilise un `LIMIT 1` sans ce départage. Cette différence déjà
documentée n'est pas supprimée pour obtenir un replay vert. Le compagnon
rejoue les lignes **héritées** aux ports Search/Content et prouve la parité du
moteur pour ces entrées ; il ne certifie pas l'équivalence des adaptateurs sur
les ambiguïtés du corpus réel.

Les quatre générations enregistrées réussissent sur le primaire ; rejet total,
retry selector et pannes/fallback ne sont pas exercés ici. Leur couverture
synthétique C3/C5/C6 reste distincte. Les absences de références ou voies vides
ne valent pas couverture de toutes les erreurs de stockage. Ce panel ne
mesure pas la qualité actuelle du goldset et ne donne pas de GO M1/M0a, ni de
preuve SSE/C7.

Validation locale finale : **924 tests API réussis, aucun ignoré**, dont
18 contrôles du compagnon et le test SQL indexé. Ruff, mypy (86 fichiers),
les quatre contrats d'import et l'auto-check historique 7/56 passent.

## Reproduire sans réseau

Après installation des dépendances du dépôt :

```bash
uv sync --all-packages --group dev --frozen
M0B_REPLAY_DIR="$(mktemp -d)"
tar -xzf tests/conformance/companions/m0b-full-20260917/m0b-full-companion.tar.gz \
  -C "$M0B_REPLAY_DIR"
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m0b_companion \
  replay "$M0B_REPLAY_DIR/m0b-full-companion/evidence"
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m0b_companion \
  check "$M0B_REPLAY_DIR/m0b-full-companion/evidence"
uv run --no-sync python -m pytest apps/api/tests/core/test_m0b_companion.py -q
```

Un nouvel enregistrement utilise `record /chemin/neuf --env-file /chemin/.env`
avec `SCW_POSTGRES_DSN_STAGING`, `SCW_POSTGRES_DSN_PROD` distincts et les clés
providers. Cette commande appelle réellement staging et les providers. Elle
refuse de réutiliser un répertoire existant ; `--limit` est réservé aux essais
partiels, qui ne peuvent pas passer le gate des sept scénarios.
