# C5 — complément d'acceptation du 15 septembre 2026

**4 scénarios enregistrés rejoués, 9 scénarios synthétiques supplémentaires et 8 contrôles négatifs réussis.**
Le sourçage des quatre réponses enregistrées a été revu manuellement : 27 unités de texte couvrent toutes les affirmations et la référence à l'article 45.
Les éléments avancés trouvent un appui dans les sources effectivement envoyées, dans le périmètre du ministère sélectionné. Deux réserves qualité sont conservées ci-dessous.

Référence exécutée : `a8bb4a0b6f78bc5777a99f494582469c198da93a` (PR #559, base `287afa03b979b24cfb69e7e6a0082fafd54f24dc`).
Ce complément ajoute des preuves et des tests ; aucun code runtime, prompt, modèle, seuil ou enregistrement d'origine n'est modifié.
Aucun accès DB ni nouvel appel provider réel n'a été effectué.

## Replays

Les quatre fixtures du [complément enregistré](../c5-recorded-20260914/README.md) ont été rejouées sur le core de la PR :
80 candidats selector, 12 sections retenues, 6 context items generator et quatre réponses exactes.
Les trois altérations de contrôle du protocole publié sont détectées.

Le [script de branches](../../../../scripts/conformance/c5_branch_companion.py) définit des sources fictives et des réponses/pannes provider explicitement **synthétiques**, puis exécute les classes historiques pour enregistrer les sorties attendues et chaque requête.
Un second processus relit ces JSON et exécute le core API avec le vrai `ChatGateway` et un transport HTTP simulé.
Les messages, température, modèle, ordre des providers et résultats doivent correspondre à l'enregistrement.

| Cas synthétique | Résultat comparé |
|---|---|
| Sélection avec doublon et plancher à 4 | Même ordre et mêmes décisions |
| Rejet explicite total, malgré le plancher | Vide, `all_rejected=true` |
| Parsing invalide | Même repli top-5 |
| Panne selector HTTP 503 | Toutes les sections conservées |
| Succès generator Albert | Même requête et réponse |
| Contexte vide sans rejet final | Appel et consignes d'insuffisance conservés |
| Albert 503 → Scaleway réussi | Même réponse, un fallback |
| Double panne 503 | Aucune réponse, deux tentatives, erreur typée |
| Contexte vide avec rejet final | Même no-answer, zéro appel provider |

Le no-answer historique provient de l'exécution réelle de la branche de `Pipeline.run`, avec les étapes antérieures et la plomberie de diagnostics simulées. L'assemblage et le retry C6 ne sont pas certifiés.
La double panne compare le contrat observable d'échec ; le changement documenté `RuntimeError` → `InferenceFailure` n'est pas présenté comme une identité des exceptions.
Une tentative par provider isole la politique de fallback ; ce protocole ne mesure pas les retries réseau réels.

Les cinq contrôles supplémentaires détectent un prompt changé, une sélection attendue changée, un provider de fallback changé, une réponse de fallback changée et un no-answer attendu changé.
Les mutations sont appliquées en mémoire après contrôle des empreintes, sans modifier les fixtures originales.

## Sourçage : revue des réponses réellement enregistrées

La revue porte sur le texte complet des réponses et sur `generator_input`, vérifié aussi contre les messages de `generator_call`.
Elle n'utilise ni le pool retrieval, ni les nouvelles sélections, ni des sources web ajoutées après la génération.
Le [détail des 27 unités](grounding-audit.json) contient les passages de réponse, les citations exactes des sources, leurs offsets et empreintes, et le jugement argumenté.
Unité = paragraphe ou puce ; plusieurs affirmations liées peuvent être étayées par plusieurs passages. Seuls quatre titres organisationnels sans affirmation sont exclus.

| Réponse | Unités | Appui observé |
|---|---:|---|
| CDD / acronyme | 1 | La fiche MATTE n°5 donne trois ans, reconduction expresse, six ans cumulés et possibilité de CDI. |
| Renouvellement / conversation | 20 | Fiche MATTE n°5 pour avenants, prévenance, RenoiRH/BRH/ESP4 ; Service-Public pour les conditions et conséquences du passage en CDI. |
| Subrogation | 2 | Article 2 enregistré : risques cités, articles 11-1 à 15 et rémunération maintenue au moins égale aux indemnités. |
| Mobilité MSO | 4 | Vademecum MSO : CDI, nécessités de service, congé non rémunéré, durées, employeur public, lettre/délai/bureau et retour de trois ans. |

« Perçoit directement les indemnités en lieu et place de l'agent » est jugé une reformulation sémantique de « subrogée dans les droits », et non une citation littérale. Aucun montant ou condition supplémentaire n'y est introduit.

### Réserves qualité conservées

1. **Renouvellement : périmètre et proportionnalité.** Le ministère enregistré est MATTE. Les formalités RenoiRH/BRH/ESP4 sont bien sourcées pour ce ministère, mais l'introduction parle de la FPE et devrait nommer cette portée locale. La rubrique de formalités ajoute aussi des procédures à une question sur les règles, malgré la consigne de proportionnalité. La phrase isolée « au-delà de six ans, CDI » doit se lire avec les conditions d'emploi permanent ouvrant droit à CDI, explicitées plus bas dans la réponse.
2. **Mobilité : complétude.** Le contexte Service-Public contient aussi le cas des emplois de direction de l'État, ouvert sous ses conditions aux CDD et CDI. La réponse décrit le cas général CDI sans cette exception. L'énoncé positif relatif au CDI est sourcé ; il ne doit pas être transformé en exclusion de tous les CDD.

Ces observations portent sur les réponses historiques conservées. Elles ne révèlent pas une divergence introduite par l'extraction ; aucune correction de prompt ni réécriture des réponses de référence n'est intégrée à cette preuve.
La fidélité aux sources sur ce panel ne certifie ni la complétude parfaite, ni la qualité globale, ni les futures réponses de modèles.

## Matrice des critères de #462

| Critère | Preuve et portée |
|---|---|
| Aucune implémentation provider dans selector/generator | Ports injectés ; frontières d'import et code C5 inchangés. |
| Replays : sélection, rejet, insuffisance, succès, fallback | Quatre cas enregistrés et neuf cas synthétiques, distingués explicitement. |
| Réponses fondées sur les sources fournies | Revue manuelle des quatre réponses, appuis exacts et réserves ci-dessus ; portée limitée au panel enregistré et à son ministère sélectionné. |
| Priorités et variantes ministérielles conservées | Prompts conservés, tests différentiels ministères/persona et requêtes exactes au replay. |
| Double panne propre | Replay via gateway : `InferenceFailure`, deux tentatives, aucune réponse trompeuse. |
| Diagnostics par requête | Tests concurrents et valeurs immuables déjà présents dans C5. |
| A5 et LEDGER à jour | Audit pré-extraction et mises à jour C5 conservés ; ce complément est référencé dans le LEDGER. |
| Aucun changement injustifié de prompt/seuil | Aucun changement runtime/prompt/seuil dans ce complément ; écarts C5 déjà documentés. |

Cette matrice étaye les huit critères **dans le périmètre d'extraction C5** et avec les limites explicites du protocole. Elle ne clôt pas l'issue avant fusion et ne change pas les gates M0b, C6 ou M1.

## Validation et reproduction

**202 tests ciblés réussis**, dont 24 tests d'acceptation, plus Ruff `E,F,I`.
Les tests CI rejouent les fixtures contre le core courant, régénèrent la référence synthétique dans un dossier temporaire pour vérifier sa reproductibilité, et contrôlent les citations/offsets/empreintes ainsi que la couverture de chaque bloc de réponse.
Ces derniers contrôles sont mécaniques : ils ne remplacent pas la revue sémantique du sourçage menée ici par l’assistant et ne sont pas un juge de qualité automatisé.

Depuis la racine du dépôt, avec Python 3.12 et les dépendances du projet :

```bash
uv run --no-sync python scripts/conformance/c5_branch_companion.py replay \
  --root . --evidence tests/conformance/companions/c5-acceptance-20260915/synthetic-evidence
uv run --no-sync python scripts/conformance/c5_branch_companion.py check \
  --root . --evidence tests/conformance/companions/c5-acceptance-20260915/synthetic-evidence
uv run --no-sync python -m pytest apps/api/tests/core/test_c5_acceptance_evidence.py -q
```

`--verify-source-hashes` vérifie également le code exact de l'enregistrement. Il est optionnel : la CI doit confronter le core courant à des entrées figées et non bloquer toute évolution de fichier par un simple hash.
Le mode `record` de ce nouveau script est exclusivement synthétique, interdit le réseau et refuse d'écraser une référence existante. Le recorder live d'origine n'a pas été relancé.

### Durcissement après relecture

La commande autonome exige les neuf identifiants de scénario attendus, chacun une seule fois avec la bonne étape. Les panels vides, incomplets, dupliqués, inconnus ou mal formés sont rejetés avant construction du client provider, même si leurs empreintes ont été recalculées. Le refus du panel vide reste actif avec `python -O`. Un ordre différent des neuf cas est accepté ; les contrôles négatifs ciblent désormais les scénarios par identifiant.

Les nouveaux enregistrements déclarent `head: null` et `source_revision_status: unverified_snapshot`, avec les empreintes des fichiers utilisés. Le recorder ne vérifie pas un arbre Git et ne doit donc pas attribuer un commit au snapshot fourni par `--root`, même si celui-ci se trouve dans un checkout. Les manifestes historiques et toutes les fixtures publiées restent inchangés ; leur provenance d'origine n'est pas réécrite.

Livrables : [replay publié](published-replay.json), [ses contrôles](published-negative-controls.json), [replay synthétique](synthetic-replay.json), [ses contrôles](synthetic-negative-controls.json), [audit de sourçage](grounding-audit.json), [empreintes](SHA256SUMS).
