# C5 — extraction selector, prompts et génération (#462)

L'extraction conserve le runtime Streamlit et le bundle M0b historique.
Le candidat API est validé par conformance différentielle synthétique ; **la
preuve enregistrée C5 reste à produire avant de déclarer le gate satisfait**.

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

## Complément enregistré préparé, non exécuté

`[record|replay]` dans `scripts/conformance/c5_companion.py` prépare une preuve
C5 indépendante. Le mode `record` nécessite une autorisation explicite pour
les lectures staging et les appels providers. Cette exécution n'a pas eu lieu.

Il prévoit exactement :

1. Vérifier et figer config + quatre cas RAG contre l'archive C4 acceptée.
2. Lire uniquement les deux prompts configurés sur le DSN explicitement nommé
   staging, distinct de production, sous TLS et transaction en lecture seule
   avec délais bornés. Aucune modification DB ni requête documentaire live.
3. Appeler le selector sur les sections C4 figées, puis le générateur sur les
   contextes finaux C4 figés. Ce sont deux preuves d'étape indépendantes : les
   sorties du nouveau selector ne reconstruisent pas un pipeline complet.
4. Enregistrer requêtes, réponses provider complètes, outputs historiques,
   snapshots de prompts, empreintes des sources et des fixtures.
5. Rejouer le core candidat hors réseau et comparer exactement requêtes,
   décisions, sections, réponses et diagnostics provider.

Les quatre cas portent sur CDD/acronyme, recherche juridique, conversation et
ministère MSO. La configuration provient du complément C4 ; le recorder ne
prétend pas vérifier la configuration actuellement déployée. Les réponses
selector/generator seront nouvelles, jamais reconstruites depuis M0b. Le
recording exige un succès primaire ; fallback, rejet total et contexte
insuffisant restent couverts par les scénarios synthétiques.

Exemple **après autorisation**, depuis la racine du checkout :

```bash
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python scripts/conformance/c5_companion.py \
  record /private/tmp/c5-evidence \
  --source /private/tmp/c4-parity-companion/evidence-v2 \
  --env-file /chemin/prive/.env

PYTHONPATH=apps/api/src python3.12 -S scripts/conformance/c5_companion.py \
  replay /private/tmp/c5-evidence
```

Le replay teste le **core du checkout courant** et rapporte ses empreintes ;
il ne suppose pas que ce checkout soit identique au candidat enregistré.
Le dossier de recording contient un journal privé à exclure de toute publication.
Les fixtures/prompt/réponses doivent être inspectés avant publication.

## Gates restants

La PR reste en brouillon jusqu'à l'enregistrement autorisé, au replay et à
l'acceptation de cette preuve C5. Les 7 fixtures / 56 artefacts M0b originaux
restent inchangés ; leur auto-check d'intégrité ne prouve pas C5. L'assemblage,
le retry et le handler non-stream restent #463/C6 ; le transport SSE reste
#464/C7. A5 et M1 ne sont pas déclarés globalement clos. Aucun déploiement.
