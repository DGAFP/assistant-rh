# C5 — extraction selector, prompts et génération (#462)

L'extraction conserve le runtime Streamlit et le bundle M0b historique.
Le candidat API est validé par conformance différentielle synthétique et par
[un complément C5 enregistré](../../../tests/conformance/companions/c5-recorded-20260914/README.md) :
**comparaison exacte réussie sur les quatre cas RAG**. La preuve permet la revue
de livraison C5 ; elle ne certifie ni l’assemblage C6 ni M1.

## Contrat livré

- `ContextSelector.select(..., today=...)` retourne sections, décisions et
  diagnostics immuables. Ordre LLM, déduplication, top-up au rang entrant,
  top-5 sur parsing invalide et keep-all sur panne provider sont conservés.
  Un rejet explicite reste vide, même avec un plancher configuré.
- `Generator.generate(..., today=...)` et son flux core retournent réponse et
  outcome provider. Les appels passent par `LLMPort`, configuré au wiring avec
  Albert primaire et Scaleway fallback. Le double échec est typé ; un échec
  partiel ne produit aucun faux succès ni texte d'erreur dans la réponse.
- `prompt_policy.py` conserve les textes, le format des sources, les politiques
  complémentaires, les citations et les variantes ministérielles. La date est
  injectée, comme en C2. Les ressources selector, generator et persona
  gestionnaire sont copiées octet pour octet.
- Le no-answer historique n'est émis que pour contexte vide **et** rejet final
  du selector, après le retry géré par C6. Un contexte vide sans rejet conserve
  l'appel LLM avec consigne explicite d'insuffisance documentaire.
- Usage effectif optionnel, provider/modèle/tentatives et prompts sont des
  données propres à l'appel. Aucun `last_*`. Une absence d'usage n'est pas zéro
  et n'est pas remplacée par une estimation.

Les changements de contrat déjà attendus par A5/B3 sont explicites : prompt
frais par appel au lieu du cache generator infini ; erreurs DB récupérables
vers ressources avec code sûr ; erreurs de programmation/configuration,
annulations et refus/échecs partiels du selector propagés. La matrice legacy
contient aussi des formes JSON inattendues qui gardent toutes les sections :
elles sont caractérisées par les tests différentiels, sans nouveau seuil.

## Preuves locales

`apps/api/tests/core/test_selection_generation.py` exécute le runtime conservé
et le candidat sur les mêmes entrées synthétiques. Comparaisons exactes des
sections et décisions, du prompt selector, des deux prompts generator et de la
réponse. Couverture des ministères, date, JSON malformé/indices/aliases,
complémentarité, plancher, rejet, ressources et persona en panne DB, absence de
sources, fallback et double panne via le vrai gateway avec transport simulé,
historique stream, interruption/annulation et isolation concurrente.

Les assertions sur les règles anti-hallucination démontrent la conservation
des consignes et des sources envoyées. Elles ne constituent pas une mesure de
qualité d'une réponse produite par un modèle réel.

`apps/api/tests/core/test_c5_companion.py` valide le recorder/replay hors ligne
avec DB/providers simulés. Il utilise les quatre entrées documentaires C4
figées, vérifie les appels exacts puis détecte : fichier altéré, prompt altéré
même après recalcul du hash, sortie attendue altérée après recalcul du hash,
et entrée C4 différente de l'archive avant tout I/O externe. Cette preuve
valide l'outillage, **pas une campagne de génération réelle**.

## Complément enregistré — 14 septembre 2026

Après autorisation explicite, `scripts/conformance/c5_companion.py record` a lu
les deux prompts staging en transaction read-only, puis enregistré huit appels
Albert réussis sur les quatre cas C4 figés. Le core exécuté est `99fc62c`.

- Selector : 80 sections entrantes, 12 conservées ; trois sélections normales
  et un repli de parsing top-5 exactement préservé.
- Generator : 6 context items C4 figés et quatre réponses exactes au replay ;
  aucune activation du fallback provider.
- Prompts complets, décisions/ordre/métadonnées, réponses et provider/fallback
  comparés exactement hors réseau. Les données d'usage provider sont conservées.
- Trois contrôles négatifs réussis sur copies : fichier altéré, prompt altéré
  après recalcul du hash, réponse attendue altérée après recalcul du hash.
- Replay autonome après extraction de l'archive avec Python 3.12 et sa seule
  bibliothèque standard. CI : les fixtures sont aussi rejouées contre le core
  du checkout courant, pour détecter les régressions futures.

Les deux étapes restent indépendantes : le generator reçoit les contextes
finaux C4, **pas** un contexte reconstruit à partir des nouveaux choix selector.
La configuration provient de C4 ; seuls les prompts et les réponses LLM sont
nouveaux. Les trois courts-circuits M0b ne sont pas comptés comme des cas C5
exercés. Rejet total, insuffisance de contexte, fallback/double panne et stream
restent couverts par les tests synthétiques.

Voir le [rapport, les sources figées et l'archive autonome](../../../tests/conformance/companions/c5-recorded-20260914/README.md)
pour les versions, empreintes, résultats, avertissement de parsing et limites.
Aucun secret ni journal privé n'est publié, aucune écriture DB ni aucun déploiement.

Pour rejouer depuis le dossier extrait `c5-parity-companion` :

```bash
PYTHONPATH=apps/api/src python3.12 -S scripts/conformance/c5_companion.py replay evidence
PYTHONPATH=apps/api/src python3.12 -S scripts/conformance/c5_companion.py check evidence
```

## Gates restants

La preuve C5 est disponible pour la revue de livraison ; l'issue reste ouverte
jusqu'à fusion. Les 7 fixtures / 56 artefacts M0b originaux restent inchangés :
ce complément repose sur de nouveaux appels et ne reconstitue pas les anciens.
L'assemblage, le retry et le handler non-stream restent #463/C6 ; le transport
SSE reste #464/C7. A5 et M1 ne sont pas déclarés globalement clos.
