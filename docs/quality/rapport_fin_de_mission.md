# Assistant RH — Rapport de mission & Résultats d'évaluation

---

## **1. Résumé exécutif**

L'Assistant RH est un chatbot IA de type RAG (Retrieval-Augmented Generation) développé pour aider les gestionnaires RH en SGCDs dans leurs recherches documentaires. Pour cette première itération, il répond aux questions réglementaires sur les agents contractuels du Ministère de la Transition Écologique (MATTE).

**Chiffres clés de la mission (septembre 2025 - février 2026) :**

* **3 itérations** du pipeline RAG, chacune guidée par les retours terrain

* **1 sprint** de test (14 testeurs, 508 questions) + **1 beta-test** d'un mois (~70 utilisateurs, 284 feedback)

* **V3 optimisée** : meilleurs scores sur toutes les métriques vs V1 (sprint) — faithfulness 0.918, relevancy 0.881, correctness 0.639, context recall 0.894

* **74,8%** des réponses V3 optimisée jugées **meilleures** que les réponses du beta-test par un juge LLM (GPT-4.1)

* **Le RAG double la justesse** des réponses par rapport à un LLM sans contexte documentaire (+122% de correctness)

***

## **2. Contexte de la mission**

### **Le problème**

Les gestionnaires RH des SGCDs traitent quotidiennement des questions réglementaires complexes : renouvellement de contrats, rémunération, congés, temps de travail, fin de contrat. Les réponses nécessitent de croiser plusieurs sources réglementaires (décrets, notes ministérielles, fiches pratiques), ce qui est un travail chronophage et sujet à erreurs.

### **La solution**

Un assistant IA conversationnel qui :

1. **Recherche** automatiquement les passages pertinents dans la documentation réglementaire (4 sources)

2. **Synthétise** une réponse structurée avec citations des textes de référence

3. **Cite ses sources** pour permettre la vérification

### **Périmètre de la première itération**

* **Ministère partenaire** : MATTE (Transition Écologique)

* **Population cible** : les contractuels

* **Sources documentaires** : fiches MATTE, fiches Service-Public, réglementation Légifrance, circulaires ministérielles, RGRH du portail BARRI

### **Calendrier**

| Date             | Jalon                                            |
| ---------------- | ------------------------------------------------ |
| Septembre 2025   | Démarrage, développement V1                      |
| 27 novembre 2025 | Sprint de test V1 (14 testeurs, demi-journée)    |
| Décembre 2025    | Analyse du sprint, développement V2              |
| 8 janvier 2026   | Lancement beta-test V2 (70 utilisateurs, 1 mois) |
| Mi-janvier 2026  | Déploiement V3 en parallèle sur le beta-test     |
| 6 février 2026   | Fin du beta-test                                 |
| 19 février 2026  | Comité d'investissement                          |

***

## **3. Évolutions techniques : 3 itérations**

### **V1 — RAG naïf (Sprint, novembre 2025)**

**Architecture :** Chunking basique → Embedding (Albert API) → Retrieval top-15 → Reranking top-8 → Génération (openweight-medium)

**Principe :** Les documents sont découpés en chunks de taille fixe, vectorisés, puis les plus proches sémantiquement de la question sont sélectionnés et rerankés avant d'être passés au LLM générateur.

**Résultats du sprint :**

* 508 questions posées, 347 évaluées

* Satisfaction : **3,4/5** (médiane 4)

* 52% de réponses bien notées (4-5/5), 26% mal notées (1-2/5)

* Points forts : clarté, pertinence quand les bonnes sources sont trouvées

* Point faible majeur : **incomplétude** (raison négative n°1, 47% des erreurs)

**Enseignements :**

* Le retriever fonctionne correctement (MATTE dans le top-15 dans 75% des cas)

* Le reranker élimine trop souvent MATTE du top-8 (forçage nécessaire dans 25% des cas)

* 58% des erreurs sont liées au retrieval (info absente ou mauvais chunks), 33% au LLM

* Le chunking basique coupe des informations essentielles (conditions, cas particuliers)

* Le modèle générateur (openweight-medium) manque de puissance pour synthétiser des contextes complexes

### **V2 — Chunking sémantique + LLM Selector (beta-test, janvier 2026)**

**Architecture :** Chunking sémantique (paragraphes entiers) → Embedding → Retrieval → Reranking → **LLM Selector** → Génération (openweight-large)

**Évolutions par rapport à V1 :**

* **Chunking sémantique** : paragraphes entiers non coupés, pour conserver le contexte

* **LLM Selector** : un modèle vérifie et filtre les chunks avant le générateur, pour réduire le bruit

* **Modèle générateur** : passage à openweight-large (plus puissant)

* **Reformulation** : prise en compte de l'historique de conversation

* **Expansion d'acronymes** : dictionnaire de 200+ acronymes RH

* **Intent-gater** : détection des questions hors-périmètre RH

**Problème identifié :** Les chunks sémantiques, souvent trop longs, contenaient l'information pertinente mais leur taille diluait le signal d'embedding, les faisant mal remonter dans le retrieval. Quand ils étaient correctement retrouvés, le générateur bénéficiait d'un contexte plus riche et produisait des réponses plus précises, confirmant l'intuition que le contexte documentaire complet améliore la génération. Le problème était donc principalement un problème de retrieval, pas de génération, et c'est ce constat qui a motivé l'architecture hybride de V3.

### **V3 — Architecture hybride optimisée (fin du beta-test)**

**Architecture :** Chunks homogènes → Retrieval top-20 → **Expansion en sections** → **Reranking sur sections** (top-10) → **LLM Selector** (sélection fine) → **ContextBuilder** (budget token dynamique, expansion documents) → ajout des références légales citées au contexte → Génération

```text
Question
    │
    ├── Reformulation (si historique de conversation)
    ├── Expansion acronymes
    │
    ▼
Retrieval parallèle (4 sources × top-20 chunks)
    │
    ▼
Agrégation en sections (groupement par section)
    │
    ▼
Reranking sur sections (top-10)
    │
    ▼
LLM Selector (sélection fine des passages pertinents)
    │
    ▼
ContextBuilder (budget token, expansion sections → documents si budget le permet)
    │
    ▼
Injection références légales
    │
    ▼
Génération (openweight-large)
```

**Évolutions par rapport à V2 :**

1. **Chunks homogènes** (comme V1) → bon recall, recherche efficace

2. **Expansion en sections** → le contexte du document est restauré (comme l'ambition V2)

3. **Reranking sur sections** (pas sur chunks) → classement plus intelligent

4. **LLM Selector** → filtre le bruit pour plus de précision en aval

5. **ContextBuilder avec budget token dynamique** → expansion au niveau document si le budget le permet

**Évolutions additionnelles :**

* Retrieval parallèle sur les 4 sources (plutôt que mélangé) → diversité garantie

* Prioritisation des sources au Selector : MATTE > Service-Public > Légifrance + RGRH

* **Normalisation markdown** des sections pour améliorer la lisibilité pour le LLM

* Références légales injectées dans le contexte avant génération

**Résultats du beta-test (V2 + V3 confondues) :**

* 445 questions posées, 264 évaluées

* Satisfaction : **3,4/5** toutes questions confondues

* **3,5/5** en excluant les questions hors champ

* **3,8/5** en excluant les cas où la base documentaire était incomplète et ne relevaient pas d’un défaut du pipeline (76%)

***

## **4. Méthodologie d'évaluation**

### **Goldsets (jeux de questions)**

| Goldset                            | Questions | Gold answer | Description                                                                                              |
| ---------------------------------- | --------- | ----------- | -------------------------------------------------------------------------------------------------------- |
| sprint_v1                          | ~500      | Non         | Questions du sprint (14 testeurs)                                                                        |
| beta_v2 / beta_v3                  | 284       | Non         | Questions du beta-test (70 utilisateurs)                                                                 |
| golden_beta                        | 159       | Non         | Sous-ensemble du beta avec feedback utilisateur, questions hors-périmètre et ‘Document manquant’ exclues |
| synthetic_docs_v1                  | 454       | Oui         | Questions synthétiques avec réponse de référence                                                         |
| Autres (factual, procedural, etc.) | ~500      | Oui         | Questions catégorisées par type                                                                          |

**Total : 1 490 questions uniques, 8 293 runs de test**

### **Métriques RAGAS (juge LLM : gpt-4.1-mini)**

| Métrique               | Mesure                                                                | Nécessite gold answer |
| ---------------------- | --------------------------------------------------------------------- | --------------------- |
| **Faithfulness**       | La réponse est-elle fidèle au contexte fourni ? (pas d'hallucination) | Non                   |
| **Answer Relevancy**   | La réponse répond-elle à la question posée ?                          | Non                   |
| **Answer Correctness** | La réponse est-elle factuellement correcte ?                          | Oui                   |
| **Context Precision**  | Les éléments pertinents du contexte sont-ils bien classés ?           | Oui                   |
| **Context Recall**     | Le contexte contient-il toutes les informations nécessaires ?         | Oui                   |

### **Juges LLM (gpt-4.1-mini)**

Pour les 159 questions golden_beta (avec feedback utilisateur réel) :

* **Juge 1 — Catégorisation d'erreurs** : analyse la question, le contexte sélectionné et la réponse générée pour identifier la source de l'erreur (retrieval_miss, context_insufficient, generator_hallucination, etc.)

* **Juge 2 — Comparaison beta** : compare la réponse V3 optimisée à la réponse qui avait été donnée pendant le beta-test, en tenant compte du feedback utilisateur

### **Configurations évaluées**

| Config       | Description                                             |
| ------------ | ------------------------------------------------------- |
| dry_no_ctx   | LLM seul, sans contexte documentaire (baseline)         |
| v1_prod      | Pipeline V1 (sprint)                                    |
| v2_prod      | Pipeline V2 (beta phase 1)                              |
| v3_prod      | Pipeline V3 (beta phase 2)                              |
| **v3_optim** | **Pipeline V3 optimisé** (top-k=50, rerank sections→20) |
| v3_reuse_*   | Même contexte V3, modèle générateur différent           |

***

## **5. Résultats**

### **5a. La valeur du RAG : LLM seul vs RAG**

Sur 469 questions avec réponse de référence :

| Configuration             | Answer Correctness |
| ------------------------- | ------------------ |
| LLM seul (sans documents) | **0.302**          |
| V3 (avec RAG)             | **0.671**          |
| **Gain**                  | **+122%**          |

Le RAG **plus que double** la justesse des réponses. Un LLM seul, même puissant, ne peut pas répondre correctement à des questions réglementaires spécifiques sans accès à la documentation.

### **5b. Progression V1 → V2 → V3**

**Comparaison équitable sur 469 questions identiques (avec réponse de référence) :**

| Config            | Faithfulness | Relevancy | Correctness | Ctx Precision | Ctx Recall |
| ----------------- | ------------ | --------- | ----------- | ------------- | ---------- |
| V1 (sprint)       | 0.809        | 0.741     | 0.545       | 0.864         | 0.651      |
| V2 (beta phase 1) | 0.645        | 0.608     | 0.493       | 0.739         | 0.525      |
| V3 (beta phase 2) | 0.893        | 0.863     | 0.623       | 0.919         | 0.833      |
| **V3 optimisée**  | **0.918**    | **0.881** | **0.639**   | **0.916**     | **0.894**  |

**Analyse :**

* **V2 en régression par rapport à V1** : le chunking sémantique a dégradé le recall (-19% de context_recall). Les chunks trop longs remontaient mal dans le retrieval à cause de la dilution du signal d'embedding. C'est ce constat qui a motivé le passage à V3.

* **V3 surpasse V1 sur toutes les métriques** : l'architecture hybride (chunks homogènes pour le recall + expansion en sections pour le contexte) combine les atouts de V1 et V2. Par rapport à V1 : +13% de faithfulness, +19% de relevancy, +17% de correctness et +37% de context recall.

### **5c. Ablation du modèle générateur**

**449 questions, même contexte (V3 prod), seul le modèle de génération change :**

| Modèle générateur               | Faithfulness | Relevancy | Correctness | Latence      |
| ------------------------------- | ------------ | --------- | ----------- | ------------ |
| openweight-large (Albert, prod) | 0.898        | 0.870     | **0.626**   | 2 921 ms     |
| mistral-medium-2508 (Mistral)   | 0.906        | 0.774     | 0.565       | 5 368 ms     |
| openweight-medium (Albert)      | 0.926        | 0.882     | 0.621       | 3 807 ms     |
| openweight-code (Albert)        | 0.903        | **0.901** | 0.580       | **2 167 ms** |
| qwen3-235b (Scaleway)           | **0.937**    | 0.880     | 0.607       | 6 346 ms     |

**Enseignements :**

* Le choix du modèle de génération a un **impact significatif** : jusqu'à +4 points de faithfulness entre le modèle de production et le meilleur modèle testé.

* **Qwen3-235B** est le plus fidèle au contexte (0.937) - il hallucine le moins.

* **openweight-code** offre le meilleur rapport qualité/prix : meilleure relevancy (0.901) et le plus rapide (2.2s).

* **openweight-large** (production) obtient la meilleure correctness (0.626)

### **5d. Retours du beta-test et analyse Golden Beta**

### **Feedback utilisateurs (beta-test, 70 utilisateurs, 1 mois)**

* **284 feedbacks** recueillis avec évaluation et commentaires

* **Raisons positives** les plus citées : Clair (35), Utile (16), Pertinent (13)

* **Raisons négatives** les plus citées : Incomplet (36), Sources manquantes (30), Confus (12)

* **Constat** : quand la question est bien dans le périmètre du beta-test et que la documentation existe, le RAG répond bien avec les bonnes sources

### **Analyse par juge LLM (159 questions golden_beta, GPT-4.1)**

**Juge 1 — Catégorisation d'erreurs sur les réponses V3 optimisée :**

| Catégorie                            | Nombre | %     |
| ------------------------------------ | ------ | ----- |
| Correct (réponse satisfaisante)      | 119    | 74,8% |
| Générateur incomplet                 | 16     | 10,1% |
| Générateur hallucination             | 10     | 6,3%  |
| Context insuffisant                  | 7      | 4,4%  |
| Générateur mauvaise interprétation   | 5      | 3,1%  |
| Retrieval miss (document non trouvé) | 2      | 1,3%  |

**74,8% des réponses sont jugées correctes**. Les erreurs restantes sont principalement des réponses incomplètes (10,1%), des hallucinations (6,3%) ou un contexte insuffisant (4,4%).

**Juge 2 — Comparaison V3 optimisée vs. réponses du beta-test :**

| Verdict                    | Nombre  | %         |
| -------------------------- | ------- | --------- |
| **V3 optimisée meilleure** | **119** | **74,8%** |
| Équivalent                 | 28      | 17,6%     |
| V3 optimisée moins bonne   | 12      | 7,5%      |

**74,8% des réponses V3 optimisée sont jugées meilleures que les réponses données pendant le beta-test.** Seules 7,5% sont jugées moins bonnes, ce qui confirme la progression réelle de la qualité.

***

## **6. Enseignements techniques**

### **Ce qui fonctionne bien**

| Composant                                   | Constat                                                                           |
| ------------------------------------------- | --------------------------------------------------------------------------------- |
| **Retrieval parallèle multi-sources**       | Meilleur que le retrieval sur chunks mélangés — garantit la diversité des sources |
| **Expansion chunks → sections → documents** | Restaure le contexte documentaire sans sacrifier le recall                        |
| **Reranking sur sections** (pas sur chunks) | Plus performant car compare des unités sémantiques complètes                      |
| **LLM Selector en fin de chaîne**           | Efficace pour filtrer quand le contenu en entrée est déjà pertinent               |
| **Normalisation markdown**                  | Pas de perte de recall, meilleure lisibilité pour le LLM                          |
| **Prioritisation des sources au Selector**  | MATTE prioritaire → meilleure pertinence métier                                   |
| **Références légales injectées**            | Enrichit le contexte sans surcharger le retrieval                                 |

### **Ce qui n'a pas fonctionné (ou pas de valeur ajoutée prouvée)**

| Composant                                    | Constat                                                                                                                                                                                                                       |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Chunking sémantique (V2)**                 | Chunks trop longs → signal d'embedding dilué → recall dégradé de -19% vs V1                                                                                                                                                   |
| **Contextualized embeddings**                | Pas de plus-value mesurable vs. embedding sur le texte brut                                                                                                                                                                   |
| **HyDE (Hypothetical Document Embedding)**   | Pas de gain mesurable sur nos goldsets actuels ; à retester sur questions juridiques uniquement                                                                                                                               |
| **Expansion d'acronymes (recall)**           | Impact marginal au retrieval ; seuls les acronymes très rares bénéficient de l'expansion.                                                                                                                                     |
| **Retrieval hybride (sémantique + lexical)** | Testé mais pas de gain mesurable sur nos goldsets actuels ; nécessiterait un goldset spécifique avec des questions à vocabulaire technique exact (références d'articles, numéros de décrets) pour mesurer l'apport du lexical |
| **Intent-gater**                             | Utile pour la fluidité conversationnelle, mais pas assez fin pour décliner les questions hors-périmètre RH                                                                                                                    |

***

## **7. Feuille de route**

### **Axe 1 — Renforcer la méthodologie d'évaluation**

L'évaluation actuelle repose sur des métriques automatiques (RAGAS) et des juges LLM. Pour aller plus loin :

1. **Évaluation humaine par des experts métier** : annotation binaire (correct / incorrect / partiel) avec catégorisation de la gravité des erreurs. 100-150 questions annotées suffisent pour un score de confiance à ±5%. La littérature montre que les juges LLM ont ~83% d'accord avec les humains — les 17% restants nécessitent un regard expert.

2. **Stratégie d'évaluation en 3 couches** : métriques auto (feedback rapide) → juge LLM (triage) → expert métier (validation finale sur les cas ambigus).

### **Axe 2 — Enrichir les données**

1. **Données RGRH** : splitter les règles individuellement (actuellement en paragraphes de bullet points, difficiles à retrouver) et créer un retriever dédié

2. **Complétion documentaire** : intégrer les annexes MATTE manquantes, les grilles IM, les notes de gestion ministérielles

3. **Acronymes ciblés** : identifier les acronymes non compris par le modèle d'embedding et les injecter au générateur plutôt qu'au retriever.

### **Axe 3 — Explorations à plus long terme**

1. **Retrieval hybride** : constituer un goldset de questions à vocabulaire technique exact (références d'articles, numéros de décrets) pour mesurer l'apport du lexical

2. **Architecture agentique** : un agent qui planifie, recherche itérativement et vérifie sa réponse. Gains potentiels importants (+10-15 pts), mais complexité accrue et latence plus élevée.

3. **Fine-tuning du générateur** : entraîner le modèle sur les paires (question, contexte, réponse experte) validées par les correcteurs métier

***

## **8. Conclusion**

L'Assistant RH a démontré sa valeur ajoutée sur le périmètre MATTE à travers 3 itérations guidées par les retours terrain :

1. **Le RAG est indispensable** : il double la justesse des réponses par rapport à un LLM seul (+122%)

2. **La V3 optimisée est significativement meilleure** que les versions précédentes : elle surpasse V1 sur toutes les métriques (+13% à +37% selon les métriques) après une V2 qui avait régressé à cause du chunking sémantique

3. **Les évaluations de comparaison le confirment** : 74,8% des réponses V3 sont jugées meilleures que celles du beta-test

4. **Les axes d'amélioration sont identifiés** et réalisables à court terme (données RGRH, acronymes, complétion documentaire)

Le projet est dans une trajectoire d'amélioration continue, avec un socle technique solide et des retours utilisateurs qui valident l'approche. L'extension à d'autres ministères permettrait d'amplifier l'impact tout en mutualisant les développements.

***

*Document mis à jour le 27 février 2026*
