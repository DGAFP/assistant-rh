# C4 — complément de parité enregistré le 14 septembre 2026

**Comparaison exacte réussie sur les quatre scénarios RAG du panel M0b.**
Ce complément est retenu pour l'acceptation C4 / #461, avec les tests différentiels
synthétiques couvrant les branches non sollicitées. Il ne remplace pas le bundle
historique M0b et ne certifie ni les autres étapes, ni l'intégration HTTP, ni M1.

## Versions et provenance

- Candidat C4 : `39d78476b243e1a7b8dd268cb5362516198368cb` (PR #558).
- Runtime conservé : package RAG de la branche staging à
  `3bd4b912b0ba731a997ddb5257fc0c10d39b62e4`, exécuté localement.
  La révision de l'image effectivement déployée n'a pas été vérifiée.
- Agrégation et builder historiques identiques entre ces deux révisions.
- Corpus staging : DSN explicitement nommé staging, distinct de production ;
  connexions TLS et sessions forcées en lecture seule, délais bornés.
- Questions : les sept scénarios publics de `queries.m0-api-parity.jsonl`.
- Aucun appel de génération finale, aucune écriture DB distante ni déploiement.
- Sources exactes, empreintes et date d'enregistrement incluses dans le manifeste.

## Preuve

Le processeur de requête et l'enchaînement historique retrieval → agrégation →
selector → contexte sont exécutés sur les questions du panel. Le recorder capture
les **entrées avant traitement**, puis les sorties attendues séparément :

- chunks complets avec métadonnées, scores non arrondis et ordre ;
- lectures SQL des sections et documents ;
- requêtes et réponses HTTP du reranker, puis son résultat brut ;
- sections reçues par le builder, donc après le selector, et configuration effective.

Les ports du candidat sont alimentés uniquement par cet enregistrement, jamais
par les sorties attendues. Le replay interdit les connexions réseau et vérifie
les empreintes des fixtures et sources. Il compare exactement sections/chunks,
scores, ordre, diagnostics historiques, context items, métadonnées, références
résolues et chaîne complète du contexte formaté.

Le selector n'est pas rejoué : sa sortie enregistrée est l'entrée du builder.
Il s'agit d'une preuve des étapes C4, pas d'une preuve de C2/C3/C5 ou du moteur assemblé.

| Scénario | Chunks entrants | Sections après reranking | Context items | Documents entiers | Résultat |
|---|---:|---:|---:|---:|---|
| CDD / acronyme | 133 | 20 | 1 | 1 | Exact |
| Subrogation / recherche juridique | 140 | 20 | 1 | 0 | Exact |
| Renouvellement / conversation | 131 | 20 | 2 | 2 | Exact |
| Congé de mobilité / MSO | 137 | 20 | 2 | 1 | Exact |

Total : **541 chunks, 80 sections après reranking, 6 context items dont 4 documents
entiers**. Quatre appels reranker de 40 candidats, tous HTTP 200. Aucun avertissement
ni erreur dans le journal d'enregistrement.

Les trois courts-circuits (salutation, demande de document, hors sujet) n'atteignent
pas C4 : statut `not_exercised`, sans les compter comme trois comparaisons réussies.
La configuration enregistrée utilise le mode wide, un budget principal de 12 000
tokens, 40 candidats au reranker et un top-k de sortie de 20.

**Limites :** aucune résolution de références ni triangulation n'a été sollicitée
dans ces quatre exécutions. Ces branches, les budgets et les pannes restent
couverts par les tests synthétiques. Les collisions de références dont le SQL
historique ne fixe pas l'ordre ne sont pas certifiées par cette capture.

## Reproduire hors ligne

Vérifier `SHA256SUMS`, puis extraire `c4-parity-companion.tar.gz` dans un dossier
temporaire. Depuis son dossier `c4-parity-companion`, avec Python 3.12 ou ultérieur :

```bash
PYTHON_DOTENV_DISABLED=1 PYTHONPATH=apps/api/src \
  python3.12 -S scripts/c4_companion.py replay evidence-v2

python3.12 scripts/check_c4_companion.py
```

Le replay utilise seulement la bibliothèque standard et les sources core incluses ;
il a été revérifié après extraction de l'archive. Il teste le candidat figé indiqué
ci-dessus, **pas automatiquement le HEAD d'un checkout futur**. Toute modification
ultérieure de C4 exige une nouvelle comparaison du candidat concerné.

- [Résultat exact](comparison.json) : `exact_comparison: true`.
- [Contrôles négatifs](negative-controls.json) : fichier altéré détecté ; score et
  prompt modifiés détectés même après recalcul de l'empreinte de fixture.
- Le paquet exclut les secrets, `.env` et le journal privé.
- Le mode `record` est l'outil utilisé dans l'environnement d'origine ; il nécessite
  les dépendances historiques et les accès staging/providers. Il n'est pas requis
  pour le replay livré et ne doit pas être lancé en CI.

## Acceptation C4

Les tests synthétiques et les cinq comparaisons SQL locales complètent les quatre
replays réels ci-dessus. Le 14 septembre, ce complément est accepté comme preuve
de parité C4 ; le bundle M0b original reste intact, avec son auto-check d'intégrité
distinct (`exact_comparison: null`). Ses entrées historiques manquantes n'ont pas
été reconstruites depuis ses sorties.

Cette acceptation ne ferme pas les gates d'intégration C6/M1 ni les cartes A5 encore
dépendantes du branchement du moteur. Aucun changement de runtime n'est inclus dans
le commit qui publie cette preuve.
