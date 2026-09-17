# C1 — contrat Chat Completions

> 2026-09-10 · [#458](https://github.com/DGAFP/assistant-rh/issues/458) · complément normatif de [l'API v1](02-api-contract.md#post-v1chatcompletions).

Ce contrat est fixé avant l'intégration C6. Il reprend la [preuve A2/#443](07-openai-client-spike.md), l'auth [B4/#456](https://github.com/DGAFP/assistant-rh/issues/456) et le catalogue [B5/#457](https://github.com/DGAFP/assistant-rh/issues/457). C1 livre les règles, exemples et cas attendus ; aucun handler HTTP, serveur supplémentaire ou `ChatService` fake/replay n'est requis. Le replay A2 existant reste une preuve de compatibilité client, pas le futur moteur.

- [C6/#463](https://github.com/DGAFP/assistant-rh/issues/463) implémente le handler non-stream, tous ses tests HTTP et la finalisation du moteur réel.
- [C7/#464](https://github.com/DGAFP/assistant-rh/issues/464) implémente le SSE et ses tests de flux/erreur/déconnexion.
- C2–C5 peuvent extraire les étapes et valider leur conformance indépendamment du transport.

## Entrée commune aux deux transports

`POST /v1/chat/completions`, corps JSON UTF-8 objet, avec `Authorization: Bearer <session opaque>` conservé côté serveur du frontend. Les règles ci-dessous s'appliquent avant tout appel au moteur, que `stream` soit vrai ou faux.

| Champ | Contrat v1 |
|---|---|
| `model` | Chaîne non vide obligatoire, sans normalisation de casse ni suppression d'espaces ; alias `assistant-rh` ou nom concret du catalogue B5. Absent, null ou autre type → 422 `invalid_request`. Chaîne inconnue → 404. |
| `messages` | Liste non vide de 32 objets maximum. Chaque objet comporte `role` et `content`. |
| `messages[].role` | Seulement `user`, `assistant`, `system`, `developer`. `tool`, `function`, rôle absent ou inconnu → 422 `unsupported_role`. |
| `messages[].content` | Chaîne ou liste de parts `{ "type": "text", "text": "…" }`, concaténées dans l'ordre sans séparateur. Null, champ absent, part mal formée ou non textuelle (image/audio/fichier) → 422 `unsupported_content`. |
| `stream` | Booléen strict, absent = `false`. Null, chaîne ou nombre → 422 `invalid_stream`. |
| `n` | Absent = 1. Seul l'entier 1 est accepté ; 0, négatif, > 1, null, chaîne, booléen ou flottant → 422 `unsupported_n`. Une seule choice. |
| `stream_options` | Absent ou null, ou objet seulement avec `stream=true`. Seul `include_usage`, booléen strict, est reconnu (défaut `false`). Mauvaise forme → 422 `invalid_stream_options` ; autre clé → 422 `unsupported_stream_option`. |
| `metadata.conversation_id` | Corrélation client optionnelle : chaîne, ou null/absent pour aucune corrélation. `metadata`, si présent non null, est un objet ; type invalide → 422 `invalid_request`. Persistée avec le run ; n'accorde aucun droit et ne recharge aucun historique. |
| `temperature`, `top_p`, `max_tokens`, `user` | Acceptés et ignorés, sans conversion ni transmission au provider. La configuration générateur vient du serveur. |
| `tools`, `tool_choice`, `parallel_tool_calls` | Acceptés et ignorés, même si le client demande un outil obligatoire ; aucune exécution, aucun `tool_calls` en sortie. Nécessaires pour `self_documentation` de `conversations`. |
| Autres champs | Champs supplémentaires du body, des messages, des parts texte et de `metadata` ignorés, sans transmission au core/provider ni journalisation automatique. Cela inclut `name`, les métadonnées de tools et les paramètres OpenAI non pris en charge tels que `max_completion_tokens`/`response_format`. Les clés supplémentaires de `stream_options` restent rejetées. |

Les champs ignorés n'acquièrent aucune sémantique OpenAI implicite : ni format JSON imposé, ni outil, ni identité tirée de `user`. Le plafond du body s'applique à tout leur contenu. Le contenu vide (`""` ou liste vide) reste un texte valide comme dans A2 ; le refus métier/no-answer appartient au pipeline. Aucun message n'est converti implicitement en chaîne.

## Question et historique déterministes

1. Valider **tous** les messages et leurs tailles, y compris ceux qui seront ignorés, puis concaténer leurs parts texte.
2. Sélectionner le dernier message de rôle `user` comme question, sans le nettoyer ni le tronquer. Aucun `user` → 422 `missing_user_message`. Tous les messages après ce dernier `user` sont ignorés pour le moteur, même un `assistant` final.
3. Dans le préfixe qui précède la question, ignorer `system` et `developer`. Ils n'entrent jamais dans les prompts ni dans l'historique du core. Les prompts système/ministère appartiennent au serveur.
4. Parcourir ce préfixe dans l'ordre avec un utilisateur en attente. Un `user` remplace le précédent utilisateur en attente ; le premier `assistant` suivant complète ce couple puis vide l'attente. Un `assistant` sans utilisateur en attente est ignoré. Un utilisateur resté sans réponse est ignoré. Les messages système intercalés ne cassent pas un couple.
5. Dans chaque réponse historique retenue, enlever le premier marqueur `\n\n---\n**Sources :**\n` et tout ce qui le suit, comme `_strip_sources` dans A2. Dans cette notation, `\n` désigne un retour à la ligne LF réel, pas deux caractères littéraux antislash + n. Ne pas supprimer un texte utilisateur ni d'autres séparateurs markdown.
6. Garder les **cinq derniers couples complets** dans leur ordre chronologique, soit 0 à 10 messages. La question courante n'est pas l'un de ces cinq tours.

Cette requête est stateless : le serveur ne complète pas l'historique à partir d'un identifiant fourni par le client. Un ancien assistant est du contexte non fiable fourni par le client, jamais une preuve d'accès aux sources.

Exemple : `S, A0, U1, U2, D, A2, A3, U3, U4, A4` donne la question `U4` et l'historique `[U2, A2]`. `S/D` sont ignorés, `U1` est remplacé, `A0/A3` sont orphelins, `U3` est incomplet et `A4` suit la question. Pour sept couples `U1/A1 … U7/A7` puis `U8`, garder `U3/A3 … U7/A7` et utiliser `U8` comme question.

## Authentification, modèle et isolation

Chaque requête utilise le resolver B4 et son groupe courant, puis `ModelService.resolve(model, group)` de B5. Ne jamais déduire le groupe/ministère de `metadata`, `user`, des messages, de l'email ou d'un identifiant de conversation. Aucun accès au corpus ni résolution de ressource avant authentification et autorisation.

| Situation | Résultat |
|---|---|
| Bearer absent, malformé, invalide, expiré (8 h), révoqué ou ancienne révision | 401 `invalid_api_key`, identique sans détail de session/groupe ; `WWW-Authenticate: Bearer`. |
| Politique valide, `model=assistant-rh` | Résoudre uniquement `default_ministry` autorisé ; renvoyer le nom **concret** dans `model` et ce ministère dans `x_assistant_rh.ministry`. |
| Modèle concret connu et ministère autorisé | Utiliser ce ministère, même s'il diffère du défaut. |
| Modèle concret connu mais interdit | 403 `ministry_forbidden`. Ne pas renvoyer la liste des ministères autorisés ni les données de groupe. |
| Modèle inconnu | 404 `model_not_found`, sans fallback sur le défaut. |
| Session encore valide mais politique corrompue (vide, ministère inconnu, défaut absent/interdit) | 500 `ministry_configuration_error`, sans fallback. Les modifications normales de politique révoquent déjà la session en B4 et donnent 401. |

Le catalogue des noms ministériels est public ; la distinction 403/404 des modèles est une décision A2/B5 conservée. Elle n'autorise aucune divulgation de ressources privées : un run/document hors groupe se traite comme inexistant (404) sur les routes dédiées. Un `conversation_id` identique entre deux groupes ne les relie pas. Prompt, corpus, run, traces et sources restent attachés au groupe/ministère résolu de chaque requête, y compris en concurrence. L'historique ne donne aucun droit documentaire.

**Amendement de preuve A2 :** le replay historique utilise `model_forbidden`. C1 retient `ministry_forbidden`, déjà livré par B4/B5 et testé avec le SDK dans `test_future_chat_resolution_errors_are_sdk_compatible`. Le statut 403 et l'exception SDK restent identiques ; C6 doit tester le code courant, sans recopier le code historique du replay. La session B4 remplace aussi le bearer statique du spike ; l'auth machine-to-machine de `conversations` reste un sujet du temps 2.

## Bornes et erreurs avant réponse

| Borne inclusive | Mesure | Dépassement |
|---|---|---|
| 1 048 576 octets (1 Mio) | Body HTTP reçu, syntaxe JSON et champs ignorés compris | 413 `request_too_large` |
| 32 messages | Liste reçue, avant toute exclusion | 422 `too_many_messages` |
| 65 536 octets (64 Kio) par `content` | UTF-8 du texte décodé et concaténé, avant nettoyage des sources | 422 `content_too_large` |
| 5 tours d'historique | Couples complets précédant la question | Fenêtre déterministe, aucune erreur |

Exemple : `"é"` répété 32 768 fois vaut 65 536 octets et passe la borne de contenu ; 32 769 fois la dépasse. La représentation JSON échappée peut peser davantage et reste soumise à la borne HTTP. Les bornes sont inclusives ; une requête exactement à la borne passe cette garde si ses autres champs sont valides.

C6/C7 rejettent une longueur annoncée > 1 Mio sans lire tout le body et bornent aussi les octets réellement reçus, sans faire confiance au seul `Content-Length` (notamment en son absence). Aucune accumulation non bornée, aucun appel au moteur ni premier octet SSE avant validation. A2 a éprouvé le chemin `Content-Length` ; l'absence de longueur et les frontières exactes sont des validations de transport **à livrer en C6**, et le proxy/buffering reste D4. Pour plusieurs erreurs simultanées, aucun ordre global n'est promis, sauf les gardes de taille et l'auth préalable à toute résolution de modèle/ressource.

Toutes les erreurs applicatives avant réponse ont la même enveloppe :

```json
{"error":{"message":"Ministry not permitted","type":"invalid_request_error","code":"ministry_forbidden"}}
```

| HTTP | `error.code` | Message sûr d'exemple |
|---|---|---|
| 401 | `invalid_api_key` | `Invalid API key` |
| 403 | `ministry_forbidden` | `Ministry not permitted` |
| 404 | `model_not_found` | `Model not found` |
| 413 | `request_too_large` | `Request body is too large` |
| 422 | `invalid_json`, `invalid_body`, `invalid_request` | `Invalid request` |
| 422 | `invalid_messages`, `invalid_message`, `unsupported_role`, `unsupported_content`, `missing_user_message` | `Invalid messages` |
| 422 | `too_many_messages`, `content_too_large` | `Request limit exceeded` |
| 422 | `unsupported_n`, `invalid_stream`, `invalid_stream_options`, `unsupported_stream_option` | `Unsupported parameter` |
| 500 | `ministry_configuration_error` | `Invalid group ministry configuration` |
| 500 | `internal_error` | `Internal server error` |
| 503 | `service_unavailable` | `Service unavailable` (stockage, comportement B4/B5 conservé) |

JSON syntaxiquement invalide → `invalid_json` ; JSON non objet → `invalid_body` ; champ obligatoire/type invalide hors cas spécialisés → `invalid_request`. Liste vide/absente/non liste → `invalid_messages` ; élément non objet → `invalid_message`. Les codes spécialisés ci-dessus sont les attentes C6, même si le handler de validation générique B4/B5 utilise actuellement `invalid_request`.

`type` vaut `invalid_request_error` en 4xx et `server_error` en 5xx. `message` est une phrase contrôlée par le serveur, non un echo du contenu ; sa langue n'est pas un discriminant client. `param` peut être absent ou un chemin de champ connu, jamais une valeur client. Les clients se fondent sur le statut/code. Réponses d'erreur : `Cache-Control: no-store`. Aucun body de validation FastAPI brut, exception, DSN, prompt, bearer, mot de passe ni détail de ressource hors scope. Le quota de login 429 reste dans B4 ; C1 n'ajoute pas de quota général chat.

Exemples d'erreurs indépendants du mode demandé :

```json
{"error":{"message":"Request body is too large","type":"invalid_request_error","code":"request_too_large"}}
```

```json
{"error":{"message":"Only n=1 is supported","type":"invalid_request_error","code":"unsupported_n","param":"n"}}
```

```json
{"error":{"message":"Internal server error","type":"server_error","code":"internal_error"}}
```

## Exemples de succès et sources

Requête avec instructions ignorées, parts texte, alias et paramètres du client `conversations` (le groupe synthétique a pour défaut `matte`) :

```json
{
  "model": "assistant-rh",
  "messages": [
    {"role": "system", "content": "Instruction client ignorée"},
    {"role": "user", "content": "Question précédente"},
    {"role": "assistant", "content": "Réponse précédente\n\n---\n**Sources :**\n1. Ancienne source"},
    {"role": "user", "content": [{"type": "text", "text": "Et pour "}, {"type": "text", "text": "un contractuel ?"}]}
  ],
  "n": 1,
  "stream": false,
  "temperature": 0.9,
  "tools": [{"type": "function", "function": {"name": "self_documentation", "parameters": {"type": "object"}}}],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "metadata": {"conversation_id": "conversation-exemple"}
}
```

Question effective : `Et pour un contractuel ?`. Historique effectif : `Question précédente` / `Réponse précédente`. Exemple d'enveloppe synthétique, sans valeur de preuve juridique :

```json
{
  "id": "chatcmpl-00000000-0000-4000-8000-000000000001",
  "object": "chat.completion",
  "created": 1755734400,
  "model": "assistant-rh-matte",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "Réponse fondée sur les sources.\n\n---\n**Sources :**\n1. Guide interne — MATTE\n2. [Fiche publique](https://example.gouv.fr/fiche) — Service-Public"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
  "x_assistant_rh": {
    "turn_id": "00000000-0000-4000-8000-000000000001",
    "ministry": "matte",
    "sources": [
      {"title": "Guide interne", "url": null, "publisher": "MATTE", "doc_ref": "guide-interne", "access": "authenticated"},
      {"title": "Fiche publique", "url": "https://example.gouv.fr/fiche", "publisher": "Service-Public", "doc_ref": "fiche-publique", "access": "public"}
    ]
  }
}
```

`created` est un timestamp Unix entier de création du run. `turn_id` est son identifiant complet, généré côté serveur ; `id = "chatcmpl-" + turn_id`. Une réponse de succès possède exactement une choice d'index 0, rôle assistant, `finish_reason="stop"`. Le modèle retourné est le modèle ministériel concret, pas le nom du LLM sous-jacent. Les compteurs d'usage proviennent de la génération si disponibles, sinon valent zéro ; ils ne promettent pas de comptabiliser tous les appels de retrieval/reformulation.

Le markdown utilise le marqueur exact d'A2 et une liste numérotée des **seules sources finales**, dans le même ordre que `x_assistant_rh.sources` et `chat_run_sources`. Chaque source expose `title`, `publisher`, `doc_ref`, `access` (`public` ou `authenticated`) et `url` (URL canonique publique ou null pour une source interne). Une URL signée/capability n'entre jamais dans le chat ni l'historique : elle se demande au clic via le [contrat documentaire A3](02-api-contract.md#accès-documentaire). Le titre/référence interne demeure affichable par un client générique sans lien cliquable.

Un refus hors périmètre ou un no-answer pour sources insuffisantes est une completion 200 contenant le texte du pipeline, sans source inventée. S'il n'y a aucune source finale, `sources=[]` et aucun bloc markdown vide n'est ajouté. Exemple de contenu : `Les sources disponibles ne permettent pas de répondre.` Cela ne transforme pas une panne technique en refus métier. La persistance atomique run/sources/traces précède le succès non-stream ; les identifiants doivent être utilisables immédiatement pour feedback/accès documentaire.

## Succès SSE et erreurs après headers

Même validation, même question/historique/scope, même contenu final et mêmes sources qu'en non-stream. Le client demande `"stream": true`, éventuellement avec `"stream_options": {"include_usage": true}`. La réponse 200 a le type `text/event-stream` ; les commentaires `: ping` toutes les ~10 s pendant l'attente ne sont ni du texte assistant ni des chunks d'usage.

Chaque événement `data:` est suivi d'une ligne vide. Les chunks JSON ont `object="chat.completion.chunk"` et les mêmes `id`, `created`, `model` pendant le run :

1. Une choice d'index 0 avec `delta.role="assistant"`, `delta.content=""`, `finish_reason=null`.
2. Des deltas `content` ; leur concaténation reconstitue la réponse et son éventuel bloc sources. Le découpage des deltas n'est pas contractuel.
3. Après persistance atomique réussie, chunk terminal avec `delta={}`, `finish_reason="stop"` et `x_assistant_rh` complet au niveau racine.
4. Seulement si `include_usage=true`, chunk `choices=[]` et compteurs `usage`, y compris zéros si indisponibles.
5. Un unique `data: [DONE]`, puis fermeture.

Voir le [framing d'exemple v1](02-api-contract.md#post-v1chatcompletions). Aucune erreur connue ne doit être suivie d'un terminal de succès ou de `[DONE]`. Si une erreur survient après headers, le HTTP reste 200 et le serveur émet, si la connexion le permet :

```text
data: {"error":{"message":"Service momentanément indisponible","type":"server_error","code":"stream_error"}}

```

Puis fermeture sans `[DONE]`, run `failed` ; le SDK lève `openai.APIError(code="stream_error")`. Une déconnexion interrompt le travail encore annulable, finalise un run `cancelled`/partiel et ne promet aucun événement livrable au client absent. Une panne de persistance ne doit pas être maquillée en succès ; elle empêche le terminal de succès. Les sources candidates/partielles ne deviennent pas des droits documentaires. Les tests de finalisation, concurrence, panne et annulation réelle appartiennent à C6/C7, pas à la preuve de replay A2.

## Matrice de validation et traçabilité

La preuve client est celle des **versions épinglées** A2 : SDK du dépôt 2.38.0 ; `conversations` 0.0.22 à `1bba2f0e444ae9c2ddb3eae68c665b63ee4a195e`, Pydantic-AI 2.22.0 et SDK 2.52.0. La [référence officielle Chat Completions](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions) vérifie la forme de l'enveloppe/chunks et du chunk d'usage ; les restrictions métier ci-dessus sont celles d'Assistant RH, pas une promesse de compatibilité OpenAI exhaustive.

Les noms `test_*` ci-dessous désignent les tests existants dans [A2](../../../tests/test_openai_contract_probe.py), [B5 core](../../../apps/api/tests/core/test_model_service.py) ou [B5 HTTP](../../../apps/api/tests/handlers/test_models_http.py). « Décision C1 » signifie un cas fixé ici qui doit devenir un test du futur transport, **pas** un résultat HTTP déjà obtenu.

| Cas attendu | Preuve existante / décision | Validation à livrer |
|---|---|---|
| Non-stream, alias résolu, enveloppe et extension accessibles | A2 `test_openai_sdk_stream_and_non_stream_contract`, `test_openai_sdk_probe_resolves_generic_model_alias` | C6 avec moteur réel |
| 7 tours → 5 derniers, orphelins et utilisateur sans réponse retirés, sources nettoyées | A2 `test_replay_ignores_client_system_messages_and_keeps_five_complete_turns` | C6 même mapping |
| `developer` intercalé, utilisateurs consécutifs, assistant après dernier user, aucun user, texte vide | Algorithme A2 `_validate_completion` lu ; exemples/décision C1 | C6 cas dédiés |
| Parts texte concaténées ; image/audio/tool/null refusés | A2 `test_replay_accepts_openai_text_part_arrays` pour le positif ; décisions et validation du replay pour les refus | C6 matrice 422 |
| Instructions système et tools ignorés ; jamais d'appel outil | A2 `run_conversations_provider_probe`, requêtes décrites dans le rapport et test de l'instance | C6/C7 avec prompts serveur |
| Paramètres ignorés, champs inconnus, corrélation non autorisante | Relecture du replay pour les champs ignorés ; sémantique metadata et types stricts fixés C1 | C6 types/absence de transmission |
| `n > 1`, option stream inconnue | A2 `test_replay_enforces_statuses_and_limits` | C6 ; `n` non entier et options invalides aussi |
| Bearer invalide, expiré/révoqué, groupe corrompu | B5 HTTP `test_models_rejects_missing_malformed_and_duplicate_bearer`, `test_sdk_rejects_unusable_sessions`, `test_authenticated_corrupt_policy_is_safe_configuration_error` | C6 même resolver B4 |
| Alias, modèle autorisé différent du défaut, inconnu, interdit, politique vide/invalide | B5 core `test_alias_resolves_only_to_configured_default_and_explicit_models_roundtrip`, `test_unknown_models_do_not_fallback`, `test_known_but_unauthorized_model_is_forbidden`, `test_invalid_policy_is_explicit_configuration_error_for_listing_and_resolution` | C6 même `ModelService` |
| 403 `ministry_forbidden` et 404 `model_not_found` compatibles SDK | B5 HTTP `test_future_chat_resolution_errors_are_sdk_compatible` ; amendement du code A2 consigné ci-dessus | C6 sur la vraie route chat |
| Limites dépassées : 33 messages, UTF-8 > 64 Kio, body > 1 Mio | A2 `test_replay_enforces_statuses_and_limits` | C6/C7 avant moteur/headers |
| Frontières exactes, taille des messages ignorés, parts cumulées, body sans `Content-Length` | Bornes A2 et décision C1 ; pas de preuve de proxy | C6 gardes HTTP ; D4 ingress |
| Erreurs JSON 401/403/404/413/422/500 et absence de détails internes | A2 erreurs/limites + B4/B5 erreurs normalisées ; 500 générique et codes spécialisés fixés C1 | C6 injection de pannes et assertions de non-divulgation |
| Sources markdown utilisables, extension inconnue tolérée | A2 SDK/provider et [test instance](../../../tests/openai-contract/conversations-instance-test.py) | C6/C7 ; adaptation sources internes dans les fronts A3 |
| No-answer 200, sources vides, cohérence markdown/extension/persistance | API v1/A3 et décision C1 ; replay A2 a une source publique constante | C6/C7 avec moteur réel |
| Pings, terminal, usage optionnel, `[DONE]`, erreur post-headers | A2 `test_replay_sse_framing_covers_pings_usage_done_and_error`, `test_post_header_error_is_an_openai_api_error_without_done` | C7 sur API réelle ; D4 sans buffering |
| Déconnexion | A2 `test_client_can_disconnect_from_stream` prouve la fermeture socket du replay | C7 annulation, run partiel et libération des ressources |
| Isolation simultanée groupes/ministères, commit avant succès, panne de commit | B4/B5 résolvent le scope ; invariants de finalisation A3/B2 | C6 concurrence/atomicité ; C7 avant terminal/`[DONE]` |

`conversations` utilise un catalogue statique, pas la découverte `/v1/models` : le déploiement doit recouper ses modèles configurés avec ceux autorisés au bearer. Pydantic-AI tolère `x_assistant_rh` mais l'abandonne ; seul le SDK le conserve dans `model_extra`. Le fork doit exposer les sources internes, associer l'id UI au `chatcmpl-*` pour le feedback et convertir l'`openai.APIError` post-headers en erreur UI. A2 n'a validé ni ces adaptations temps 2 ni leur authentification finale.

Validation C1 : revue des décisions contre le code/rapport A2 et les contrats livrés B4/B5 ; vérification des exemples JSON et liens locaux ; réexécution des tests existants consignée au [LEDGER](LEDGER.md). Les résultats historiques local/homelab de l'instance `conversations` restent ceux du 2026-09-02, sans prétendre à une nouvelle exécution ni à un déploiement staging/production.
