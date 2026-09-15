# C5 — complément enregistré le 14 septembre 2026

**Comparaison exacte réussie sur les quatre scénarios RAG enregistrés.**
Cette preuve complète la conformance synthétique de #462. Elle porte sur les
étapes selector et generator prises séparément ; elle ne remplace pas M0b et
ne certifie ni l'assemblage C6, ni M1, ni la qualité globale des réponses.

## Versions et provenance

- Core candidat et runtime conservé : checkout `99fc62c620ad3ca0994609218cc01c5fbf7dc87a`
  (PR #559). Les sources exécutées sont figées avec leurs empreintes dans l'archive.
- Entrées documentaires et configuration : [complément C4 accepté](../c4-staging-20260914/README.md).
  Config + quatre fichiers de cas ont été comparés octet pour octet à cette
  archive puis figés en mémoire **avant tout accès externe**.
- Deux prompts : lectures fraîches de staging, via DSN explicitement nommé
  staging et distinct de production, TLS et transaction forcée en lecture seule,
  délais bornés. Aucun accès documentaire live et aucune écriture DB.
- Appels nouveaux : 4 selector Albert `openweight-large`, puis 4 generator
  Albert `deepseek-v4-flash`, température 0. Succès primaire pour les 8 appels ;
  aucun fallback provider. Les configurations sont celles de C4, sans prétendre
  vérifier la configuration de l'image actuellement déployée.
- Fin d'enregistrement : **2026-09-14 à 14:22:47 UTC** ; date injectée `2026-09-14`.
  Le lancement utilise l'autorisation explicite de l'utilisateur.

Les requêtes complètes et réponses provider ont été enregistrées **avant**
les décisions du selector et du generator. Le candidat reçoit uniquement les
réponses de ces appels, jamais des réponses reconstruites à partir d'outputs
attendus. Le replay vérifie chaque requête exacte et sa température avant de
restituer l'issue LLM enregistrée, puis compare les sorties à celles du runtime
conservé.

## Résultats

| Scénario | Candidats selector | Sections conservées | État selector | Context items generator | Réponse (caractères) |
|---|---:|---:|---|---:|---:|
| CDD / acronyme | 20 | 5 | Repli parsing top-5 | 1 | 306 |
| Renouvellement / conversation | 20 | 3 | Sélection | 2 | 2 690 |
| Subrogation / recherche juridique | 20 | 2 | Sélection | 1 | 806 |
| Congé de mobilité / MSO | 20 | 2 | Sélection | 2 | 988 |

**80 sections candidates, 12 conservées, 6 context items de génération.**
Comparaison exacte des sections et métadonnées, de l'ordre, des décisions et
raisons selector, des prompts complets, des réponses finales et diagnostics
provider/fallback. Les réponses et les données d'usage réelles figurent dans les
fixtures ; aucune estimation d'usage n'est substituée aux valeurs provider.

Le cas CDD contient une réponse selector non reconnue par le parsing historique.
Les deux runtimes conservent exactement les cinq premières sections et les
mêmes diagnostics. Cet avertissement connu est conservé dans la preuve, sans
corriger opportunément le parser ni déclarer ce cas comme une sélection normale.

## Limites

- Les deux étapes sont **indépendantes** : le generator reçoit le contexte final
  C4 figé, pas une reconstruction du contexte depuis les nouveaux choix selector.
  Le cas conversation emploie la question déjà traitée de C4 ; le generator
  non-stream conserve son contrat historique sans historique de messages.
- Aucun rejet total ni fallback provider réel n'a été provoqué. Rejet/no-answer,
  contexte insuffisant, double panne, fallback et stream restent couverts par
  les tests synthétiques de la PR. L'unique repli réel est le parsing top-5.
- Les trois courts-circuits du panel M0b n'atteignent pas C5 et ne sont pas
  comptés comme des replays réussis. Aucune reconstitution de leurs inputs.
- Cette comparaison mesure la conservation du comportement, pas une nouvelle
  évaluation de qualité, de corpus courant ou de runtime déployé. Aucun déploiement.

## Reproduire hors ligne

Vérifier `SHA256SUMS`, puis extraire `c5-parity-companion.tar.gz` dans un dossier
temporaire. Depuis le dossier extrait `c5-parity-companion`, avec Python 3.12+ :

```bash
PYTHONPATH=apps/api/src python3.12 -S scripts/conformance/c5_companion.py replay evidence
PYTHONPATH=apps/api/src python3.12 -S scripts/conformance/c5_companion.py check evidence
```

La bibliothèque standard suffit. Le réseau est interdit pendant replay/check.
Les trois contrôles négatifs détectent un fichier altéré, un prompt modifié même
avec son nouveau hash et une réponse attendue modifiée même avec son nouveau hash.
Ils opèrent sur des copies temporaires et ne modifient pas la référence.

- [Comparaison exacte](comparison.json).
- [Contrôles négatifs](negative-controls.json).
- [Empreintes des livrables](SHA256SUMS).

L'archive contient la version figée du core et des sources historiques ainsi que
l'outil de replay. Le `record` doit être exécuté depuis le checkout complet,
avec le complément C4 disponible et les accès privés autorisés ; il n'est pas
nécessaire pour consommer cette preuve. Aucun `.env`, DSN, clé ou journal privé
n'est inclus. Les valeurs secrètes configurées ont été recherchées dans les
fixtures avant publication : aucune correspondance.

La CI de la PR extrait les fixtures publiées et les rejoue sur le **core du
checkout courant**, pas sur le core figé dans l'archive. Une modification future
sera donc confrontée à cette référence, en plus du replay autonome du snapshot.
