# C6 — pipeline réel et Chat Completions non-stream

Issue [#463](https://github.com/DGAFP/assistant-rh/issues/463), base d'implémentation
`cb76fe3` du 15 septembre 2026. Contrat : [C1](09-chat-completions-contract.md).

## Assemblage

`handlers/chat_runtime.py` compose les vrais adaptateurs B2/B3 et les étapes
C2–C5. Le lifespan possède le pool PostgreSQL et le client HTTP providers
(`trust_env=False`) ; il ferme le client avant le pool. Chaque appel charge une
configuration serveur immuable, résout le modèle dans le groupe B4/B5 et crée
son `RunContext`, sa date Europe/Paris et deux UUID v4 complets. Aucun champ
`last_*` ni singleton métier ; le circuit embeddings ne contient que des
compteurs techniques.

Le pipeline conserve le gate direct, le scope ministère + Service-Public +
DGAFP, le DGAFP hybride, l'ordre des étapes, les politiques C2–C5 et le retry
selector. Le retry vide après rejet initial conserve le no-answer. C1 retient
cinq couples complets ; C2 applique ensuite sa fenêtre historique de quatre
couples dans son prompt. Le générateur **non-stream** conserve l'absence
d'historique du runtime retenu. Aucun prompt ni seuil n'est changé.

Le transport valide le body entier, y compris les messages ignorés, avant le
moteur. Il borne les octets annoncés et reçus à 1 Mio, refuse plus de 32
messages et plus de 64 Kio UTF-8 par contenu. Auth B4 et résolution B5 sont
réutilisées ; les paramètres ignorés ne sont transmis ni aux prompts ni aux
providers. Les erreurs sont des enveloppes JSON sûres, sans détail interne.
`stream=true` est temporairement rejeté avec 422 `invalid_stream` ; son
transport appartient à C7/#464.

## Finalisation et sources

La finalisation n'ouvre une transaction qu'après les appels providers.
`ChatRunStore.finalize` insère le run, toutes ses sources ordonnées et ses
événements ; aucun succès HTTP ne précède le commit. Les statuts `completed`,
`failed`, `cancelled`, les diagnostics, l'éditeur et l'accès des sources sont
conservés dans `api_record` existant, sans nouvelle migration.

Les sources sont dérivées uniquement des context items effectivement servis,
dédupliquées par document dans leur ordre final. Un UUID documentaire est
prioritaire sur le short ID pour reconnaître le même document servi entier
et par section. Les titres, éditeurs et références sont exposés en JSON et
Markdown ; les références sont les mêmes que dans `chat_run_sources`.
Les liens canoniques HTTPS Service-Public/Légifrance sans query, fragment ni
credentials restent publics. Les autres liens sont omis (`url=null`, accès
authentifié), y compris les URL S3 signées. L'accès aux documents au clic reste
une route ultérieure ; aucune source candidate ne donne de droit documentaire.

Une panne avant finalisation produit un run `failed` sans source finale. Une
panne transactionnelle du succès annule le parent et ses enfants, puis tente
d'enregistrer l'échec avec le même ID. Si le stockage reste indisponible,
l'API retourne une erreur et un log de corrélation sûr : aucune durabilité
n'est revendiquée dans ce cas. Une collision refuse l'insertion, sans
écrasement ni nouvelle attribution de source. Un commit dont l'issue réseau
est indéterminée n'est jamais annoncé comme succès.

## Adaptations explicites par rapport aux étapes / B2

- C1 exige `id = "chatcmpl-" + turn_id`. Les nouveaux runs stockent donc un
  UUID v4 hex complet sans préfixe ; les anciens IDs B2 préfixés restent
  lisibles/acceptés par le store et leur générateur historique reste inchangé.
  Les futures routes feedback/documents résoudront le préfixe du completion ID
  vers ce `turn_id` ; elles ne sont pas livrées par C6.
- Le signal `Retriever.embedding_failed` conservé en C3 devient un **échec
  technique C6**, pour l'essai initial comme pour le retry. Générer un succès
  sans contexte après une double panne serait contraire à C1. Le résultat C3
  conserve aussi les tentatives providers échouées ; le signal vide et les
  règles de retrieval restent inchangés.
- Le Markdown C1 déduplique les sources documentaires finales ; les sorties
  internes de C2–C5 restent disponibles dans les événements de trace.

## Interfaces C7

`EventSinkPort.publish(PipelineEvent)` fournit des événements par étape et
essai, corrélés au run, avec backpressure. `CancellationPort.checkpoint()`
permet l'annulation coopérative entre étapes. Une annulation de tâche coupe
l'attente provider et finalise `cancelled` sans source finale. Le contexte
réserve `partial_answer` et le diagnostic `partial` pour le streaming.
Le transport SSE, les files/workers/pings, le pilotage de la déconnexion et
le shielding de finalisation sous un cancel-scope de transport restent C7.
Les traces d'étapes contiennent des projections explicites des identifiants,
scores, décisions, prompts et usage ; elles ne sont jamais incluses dans la
réponse publique. Les résultats complets restent disponibles au moteur.

## Corrections de revue du 17 septembre 2026

La persistance ne sérialise plus automatiquement tous les champs des résultats.
`core/pipeline/trace_projection.py` choisit les champs de chaque étape ;
`core/trace_values.py` filtre les URL HTTP(S) non publiques, même dans un prompt,
puis borne les textes à 4 096 caractères et les collections à 40 éléments.
Une troncature est signalée explicitement. Si le JSON ainsi projeté dépasse
64 Kio, le payload est remplacé par un marqueur de dépassement avec sa taille.
Ces limites s'appliquent aux sorties/diagnostics de trace, jamais aux entrées
providers, à la réponse servie ni à la liste des sources finales.

Les traces conservent les références et scores avant/après reranking, mais ne
dupliquent plus le texte intégral de chaque chunk/section ni les vecteurs
d'embedding. Ce sont des diagnostics opérationnels bornés, pas un bundle de
replay intégral ; les entrées complètes de conformance doivent rester des
artefacts dédiés. Le gate M0b demeure ouvert.

Une double panne embeddings conserve désormais provider, modèle, type d'erreur
et statut HTTP dans l'exception, le run et l'événement échoué, y compris lors du
retry selector. Les tests couvrent aussi l'absence d'URL signée dans le record
persisté et l'absence de modification du prompt réellement envoyé au provider.

La politique et le rendu des sources vivent dans `core/sources.py`. Les
constructeurs du run et du câblage utilisent des arguments nommés ; la tentative
retrieval/contexte et l'extraction question/historique ont des fonctions dédiées.
Les commentaires locaux expliquent les invariants plutôt que la chronologie
du chantier.

Validation de cette correction : **905 tests API réussis, aucun ignoré**, dont
166 tests DB sur un conteneur local jetable `pgvector/pgvector:pg17`. Ruff,
mypy (86 fichiers) et les trois contrats d'import passent. L'auto-check M0b
reste inchangé : 7 fixtures, 56 artefacts, `exact_comparison: null`.

## Configuration providers

Albert utilise `ALBERT_API_KEY`, `ALBERT_BASE_URL`, `ALBERT_EMBED_MODEL`
(défaut `openweight-embeddings`) et `ALBERT_RERANK_MODEL` (défaut
`openweight-rerank`). Les modèles de classification, selector et génération
viennent de la configuration serveur existante. Avec `SCALEWAY_API_KEY`, le
fallback utilise `SCALEWAY_BASE_URL`, `bge-multilingual-gemma2` pour embeddings
et le modèle fallback de `GenerationConfig` pour la génération. Sans cette
clé, Albert reste utilisable et aucun fallback Scaleway n'est construit.
Aucun fichier `.env` n'est lu par la composition ; l'environnement est fourni
par le processus qui démarre l'API.

## Matrice de preuve

Validation du 15 septembre 2026 : **898 tests API réussis, aucun ignoré**, sur
PostgreSQL 18.4 + pgvector 0.8.2 synthétiques locaux. Suite historique :
**1 442 réussis, 46 ignorés** (1 428 sous sandbox et 14 tests A2 avec serveur
loopback local). Ruff sur les chemins CI, mypy (83 fichiers) et les trois
contrats d'import passent. Revue indépendante finale favorable après correction
du cas de double panne embeddings. Données de fuseau horaire déclarées en
dépendance runtime, testées sans base de fuseaux système.

Docker indisponible localement : la construction d'image et le smoke Compose
restent à vérifier en CI sur le commit publié.

| Critère #463 | Preuve locale |
|---|---|
| Conformance historique intégrale M0b | **Ouverte** : entrées brutes manquantes, aucune reconstruction |
| Pas de singleton / `last_*` métier | Core sans état de résultat, tests d'import et concurrence |
| Isolation groupe/ministère | `test_concurrent_ministries_do_not_share_prompt_source_result_or_traces` et tests HTTP B4/B5/C6 |
| IDs complets | UUID v4, égalité enveloppe/run, tests collision du vrai store |
| Succès / échec / annulation persistés | `test_chat_service.py`, `test_chat_finalization.py` sur PostgreSQL |
| HTTP branché au vrai service | `test_chat_runtime.py` : HTTP → production composition → DB réelle, provider wire simulé |
| SDK OpenAI | Client épinglé du dépôt en validation stricte ; enveloppe, usage, extension et erreurs réelles |
| Validation C1 | `test_chat_http.py` : historique, types, bornes inclusives, flux sans longueur, modèles et non-divulgation |
| Atomicité | Contrainte SQL violée après première source, rollback complet, run failed ; attente HTTP du commit |
| Interfaces C7 | Événements, annulation coopérative et annulation de tâche ; pas de preuve SSE |
| Carte A5 / LEDGER | [Carte d'entrée C6](07-runtime-isolation-audit.md#carte-dentrée-c6--orchestration-2026-09-15-cb76fe3), entrée C6 au LEDGER |

Reproduction sur une base **synthétique locale dédiée** portant exactement le
nom `assistant_rh_api_test` :

```bash
uv sync --all-packages --group dev --frozen
API_SYNTHETIC_POSTGRES_DSN='postgresql://assistant_rh_api:assistant_rh_api@127.0.0.1:55463/assistant_rh_api_test?sslmode=disable' \
  uv run --no-sync python -m pytest apps/api/tests -q
uv run --no-sync ruff check apps/api/src apps/api/tests
uv run --no-sync mypy apps/api/src/assistant_rh_api --ignore-missing-imports
uv run --no-sync lint-imports --config apps/api/pyproject.toml --no-cache
uv run --no-sync python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0
```

Le test de composition simule uniquement le fil HTTP des providers. Il ne
mesure ni la qualité RAG actuelle ni la parité avec la réponse d'un modèle
live. Le bundle M0b original n'a pas été modifié ; son auto-check d'intégrité
ne prouve pas le replay du nouveau moteur. C6 reste en brouillon et #463
ouverte pour cette preuve. Aucun enregistrement staging/provider réel,
déploiement, migration distante ou GO M1 n'est réalisé.
