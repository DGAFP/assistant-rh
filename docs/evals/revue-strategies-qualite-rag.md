# Revue des stratégies d'amélioration de la qualité RAG — état des connaissances

**Période couverte** : 2026-07-15 → 2026-07-22.
**Référentiel de mesure** : goldset `baseline_v1` (99 questions réelles résolues), protocole à deux étages : **screening** par `x-ai/grok-4.5` via OpenRouter avec ZDR stricte (`provider.data_collection=deny` **et** `provider.zdr=true`) ; **gates d'adoption** par Scaleway `qwen3-235b` souverain en vote majoritaire à 3. Référence officielle : runs 118/123/124 re-jugés sous Scaleway maj-3 (**0,677**, 26 échecs stables) ; détail dans le journal.
**Méthode** : investigation multi-agents du 17/07 (autopsie empirique des 34 échecs du run 116, variantes de retrieval rejouées hors-ligne, revues de code, synthèse contradictoire par panel adversarial à 3 lentilles), puis **sondes ciblées** — chaque stratégie est éprouvée offline sur les échecs réels *avant* toute implémentation. Ce document est la source de vérité stratégique ; le détail run-par-run vit dans `journal-experimentations-rag.md`.

> ⚠️ Les rapports bruts des sondes (fichiers de travail de session) ont été purgés avec le scratchpad temporaire le 21/07 ; les chiffres ci-dessous sont la trace consolidée. Les données d'éval citées restent en base (runs 112-145).

---

## 1. D'où viennent les échecs — le funnel mesuré (34 échecs du run 116)

Décomposition établie par re-jeu hors-ligne étage par étage, corrigée par le panel adversarial :

| Cause racine | n | Nature |
|---|---|---|
| Artefacts de juge (429 OpenRouter, jamais jugées) | 2 | mesure — q228 pur artefact (PASS au re-juge), q660 échec réel (`incomplete`) |
| Goldset : sources fausses (gold answer non dérivable du doc pointé) | 5 | mesure/goldset |
| Goldset : articles 86-83 **abrogés en 2025** (codification CGFP) | 3 | goldset — **pas** un trou d'ingestion (exclusion par design, `reconcile.py`) |
| Intent gating (retrieval jamais exécuté) | 2 | query_processor |
| Pré-filtre du reranker `_MAX_RERANK_INPUT=20` (la section-réponse n'a jamais VU le reranker) | 3 | rerank (plomberie) |
| Misses profonds d'espace d'embedding (gold au rang 58-463 en sémantique) | 7 | retrieval — fossé de vocabulaire question-métier ↔ texte juridique |
| Near-miss ANN (probes) | 1 | retrieval |
| Retrieval fin (doc servi, section-réponse absente des ~120 chunks) | 3 | retrieval section |
| Génération (la section-réponse était SERVIE, réponse fausse/incomplète) | 8 | generator — mésinterprétation juridique, complétude |
| **Selector** | **0** | innocenté (0/20 : ne jette jamais la section-réponse) |
| **Ingestion (couverture corpus)** | **0** | vérif PISTE exhaustive : 4 165/4 165 cids, 25 textes à 100 % |

Enseignement structurant : ~10/34 étaient des échecs de **mesure** (juge/goldset), pas de pipeline ; le reste se répartit entre **entrée du retrieval** (vocabulaire), **plomberie du rerank** et **génération**. Le selector et l'ingestion, longtemps suspects, sont hors de cause.

---

## 2. Catalogue des stratégies

Statuts : 🔴 **réfuté par la mesure** · ⚫ écarté par analyse · 🟢 **validé, à construire/adopter** · ✅ livré · 🔵 test en cours · 🟡 candidat non testé.

### 2.1 Côté requête (query)

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| **Multi-query** (paraphrases LLM + fusion RRF) | 🔴 | Rejoué offline sur les 14 échecs doc-rank : 2/14 dans le top-30 (= identique au semantic seul) ; **0 conversion bout-en-bout** après rerank ; **+3,36 sections near-miss/question** (carburant d'hallucination) ; dégrade même q30 (rang rerank 9→13). Dossier clos sur `baseline_v1`. |
| **HyDE** | ⚫ | Pseudo-document qui hallucine des références de décret = drift lexical vers les mauvais textes ; bénéfices mitigés en benchmark ; flag `enable_hyde` mort à purger. |
| **Routage référence-juridique** (détection « décret X, art. Y » → lookup direct) | 🔴 | Sonde : **0/18 questions du panel contiennent une référence détectable** — les références sont dans les *golds*, pas dans les questions des agents (qui parlent métier). Sans objet sur ce goldset. |
| **Fix intent gating** (gater la réponse, pas le retrieval) | 🟢 | q4535 classée `out_of_scope` alors que le retrieval la met au rang 1-2 (mesuré). +0,010. À livrer **avec** le gate d'abstention (vague 2), pas avant — sinon le trafic dé-gaté n'a que le prompt comme garde-fou. |
| Décomposition de question (bi-concept) | 🟡 | Non testé. Le goldset sous-représente les questions bi-concept (« astreintes + temps partiel ») pourtant présentes dans les feedbacks testeurs. À re-poser quand le goldset v2 (programme C) existera. |
| Classification de thème pour **router/filtrer** | ⚫ | En filtre dur : crée mécaniquement un nouveau mode d'échec (mauvais thème → miss garanti). Acceptable uniquement en **boost soft** — couvert par la couche de représentation (V3/thèmes, cf. 2.3). Le `query_processor` calcule déjà un thème, inutilisé par le retrieval. |

### 2.2 Côté recherche (retrieval)

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| **Hybrid RRF par défaut** (sémantique+lexical) | 🔴 | 2/14 doc-rank top-30, 0 conversion nette bout-en-bout, +2,86 near-miss/q. Reste tel quel là où il est déjà (dgafp forcé, retry) — dans la baseline mesurée. |
| **RRF pondéré** (70/30, 30/70) | 🔴 | 2/14 et 1/14 — aucune pondération ne bat le semantic seul sur les échecs. Pas de sweep alpha. |
| **initial_top_k 30→50** | 🔴 | La bande de rangs 31-50 est **vide** (0/14) : élargir ne récupérerait rien. Les misses sont aux rangs 58-463 (problème d'espace, pas de seuil). |
| `ivfflat_probes` 5→15/20 | ⚫ | Déjà tranché (15 sur-bruité) ; seule q30 en bénéficierait, mieux traitée par le rerank. |
| **Graphe de renvois juridiques** (expansion déterministe 1 saut) | 🟢 | Le droit se cite explicitement (« dans les conditions prévues par le décret 84-972 »). Sonde : expansion **bidirectionnelle plafonnée** (≤20/renvoi, ≤15 citeurs) = **6/34 conversions, bruit 22 chunks/q** (9/34 sans plafond ; 26-39 % du sous-ensemble juridique). **La moitié des conversions viennent du sens arrière** (« qui cite le top-30 »). Source : liens PISTE `lien_citations` déjà ingérés (16 636) mais figés à la version d'origine → union avec regex (précision 20/20 vérifiée). Design : table `rag_renvois_edges` à l'ingestion + JOIN post-retrieve, bruit trié par le rerank existant. Zéro LLM. **Replay bout-en-bout (21/07)** : le GO doc-level se dégonfle au vrai reranker — **2 conversions fermes (q220 rang 2/0.924 via pont X-CORPUS matte→84-972 ; q229 rang 9/0.197) + 1 gain complétude (q191)** ; q194 réfutée (rang 26, voie=R2) ; le sens ARRIÈRE ne convertit rien ; déclenchement par bandes de score (CRAG) RÉFUTÉ (conversions sur pools ≥0.87) → toujours-on ou rien. Attente recalibrée **+0.02-0.03 (sous σ)** — spec ferme livrée (src multi-tables, quota réservé à l'entrée du rerank). |
| Boucle agentique de recherche (RAG-as-tool, ReAct multi-turn) | 🔴 | Sonde 18 questions, budget 6 appels adaptatifs : qwen plafond **1/7** misses profonds (hit non reconnu au stop), Albert prod (gpt-oss-120b) **0/7**. Détail accablant : qwen déclare `found=true` sur **ses 7 échecs** (biais de complaisance mesuré). Albert : protocole JSON parfait (0/110 tours), 10× plus rapide, recherche plus faible. Le mur est le vocabulaire à l'entrée — reformuler en boucle ne le franchit pas. Vérifié adversarialement (34/34 hits recontrôlés, 21/21 rejoués en base). |
| Sentence-window retrieval | ⚫ | Redondant avec l'agrégation chunks→sections existante ; coût de ré-indexation élevé. |
| Upgrade embeddings / self-host | ⚫ (court terme) | Albert n'expose que bge-m3 ; upgrade = auto-hébergement + ré-embedding complet pour un gain non prouvé sur nos échecs, alors que le structurel (représentation, renvois) n'est pas épuisé. Dernier recours post-vague 3. |

### 2.3 Côté représentation du corpus (ingestion) — **le chantier principal**

Constat moteur : le corpus Service-Public (structuré en Q&A) surperforme ; le corpus juridique non. Les misses profonds sont un problème de **représentation**, pas de recherche. Sonde « couche de représentation » (174 enrichissements générés par Albert **sans voir les questions**, rangs simulés contre le ladder des 600 candidats, contrôle V0 parfait cos=1.0) :

| Variante | Statut | Preuve / décision |
|---|---|---|
| **R2 — résumés d'ARTICLE en langage métier, unités d'index ADDITIVES** | 🟢 **vaisseau amiral** | q194 : rang 122→**25** ; q229 : 183→**24** ; projection prod **5/7 misses profonds récupérés**. **Contrôle différentiel PASS** : gold enrichi +0,02 de similarité pendant que les 10 concurrents enrichis perdent −0,03/−0,05 (le levier est réel, pas un artefact). Additif = zéro régression possible, réversible. Principe anti-hallucination : **le résumé TROUVE, il ne DIT jamais** (le générateur ne reçoit que le texte juridique authentique du parent). Pipeline : ~4 378 résumés, ~5,1 M tokens in / 1,5 M out, ~5 h Albert, cache versionné (pattern page_vision), delta par checksum. Périmètre v1 : dgafp, puis PDF ministères. **Pilote livré (21/07)** : branche `feat/r2-article-summaries` (design lignes additionnelles : embedding=résumé, chunk_text=texte authentique, `index_variant` versionné ; 28 tests) ; **101/101 articles pilotes acceptés**, garde anti-invention efficace (un « 90 % » inventé reformulé en vague) ; coût réel = **moitié de l'estimation** (~2,66 M in / 0,60 M out, ~3 h). Reste : revue humaine → génération corpus → insertion gatée → A/B. |
| R5 — préfixe combo (contexte+thèmes+questions+texte, **remplace** l'embedding) | 🟡 réserve | Fort (q221 : 170→72 ; q30 : 183→78) mais **régressions mesurées** sur des chunks déjà bien classés (q4531 : 4→74 en variante contexte). 2ᵉ vague, corpus ciblés, derrière A/B complet. |
| V1/V3/V4 — préfixes isolés (contexte seul / thèmes seuls / questions seules) | 🔴 | Variance toxique, régressions. Ne jamais déployer isolément. |
| R6 — résumé par CHUNK autonome | 🟡 non testé (par choix) | Grain fragment (reproduit le problème) + ~45 000 unités (coût ×10) pour un bénéfice que R2 capture. Testable en ~30 min si demandé. |
| Enrichissement lexical/alias des chunks (titres, vocabulaire métier) | 🟡 | Partiellement subsumé par R2. Indice favorable : l'unique hit de la sonde agentique (q30) venait d'un **titre de document**. |
| **Dédup chunking Service-Public** (Q_ONLY/QA_COMPOSITE quasi-doublons) | 🟢 | Co-facteur mesuré de q17 (chunk « question seule » servi au rang 1) et q23 (seule la section-titre servie) ; sature le top-30. Rebuild complet du corpus SP requis. |
| Chunking section-size-aware (petite section = 1 chunk) | 🟡 | Diagnostiqué le 08/07 (chunks quasi-identiques bruitant le reranker) ; partiellement recouvert par la dédup SP. À re-prioriser après vague 3. |
| Fix OCR schémas (page-vision VLM sur pages à risque) | ✅ | Livré (PR #320, `pvlogic3`) : slide 57 CONTRAT/AVENANT reconstruite, MASA re-traité. Garde de fidélité anti-hallucination (recall vocabulaire + borne de croissance). |
| Couverture corpus (articles « manquants ») | ✅ (non-sujet) | Zéro trou : les 17 articles 86-83 absents sont juridiquement **abrogés** (codification CGFP 2025), le contenu existe sous R331-x ; les LEGIARTI « absents » étaient des ids de version pré-migration #289. Le vrai fix était côté goldset (fait). |

### 2.4 Côté rerank

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| **Découpler l'entrée du rerank** : `v3_rerank_input_k=40` (sortie top-20 inchangée) | 🟢 vague 1 | Le pré-filtre à 20 est le coupable direct de 3 échecs. Contrefactuel Albert full-pool : q17 → rangs 2-4 (scores 0,9996+), q192 → rang 1 (0,606). q218 non réparé (rang 25). Containment mesurée, conversion générateur à confirmer en A/B. |
| Texte de rerank = heading + best-chunk (au lieu de `markdown[:1500]` tronqué) | 🟡 | Pointé par revues de code (q30 rang 9-13 lié à la troncature). A/B obligatoire (change tous les rangs). |
| Rerank au niveau chunk (flag `enable_chunk_reranker`, jamais implémenté) | 🟡 réserve | Cible q218 (rang section 25 en full-pool). Re-sémantise les poids d'agrégation, +0,3-1 s. |
| **Gate mécanique d'abstention** `v3_rerank_score_threshold=0.20` | 🟢 vague 2 | Les scores du reranker Albert séparent les pools vides (échecs doc-rank : médiane ~0,2) des pools sains (passers : médiane ~0,97). À t=0,20 : **6/12 pools pauvres abstenus, 1 seul passer cassé/65** (q214, pool à 0,054). Conditions : uniquement si `reranker_status==completed` ; neutraliser le retry-hybrid post-abstention (il réinjecte le near-miss mesuré). Valeur = **garantie anti-hallucination** (wrong_law → refus sourcé), pas du judge_pass. Ne couvre PAS le bucket « bon doc servi » (non séparable, par construction). |
| Remplacement du modèle de rerank (self-host) | ⚫ (court terme) | Le modèle est **innocenté** : quand il voit la section-réponse, il la classe 1-4 à 0,9996. Ses échecs = plomberie (entrée 20, troncature). Re-poser la question si le résidu post-vague 1 le désigne. |

### 2.5 Côté selector & contexte

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| Refonte / changement de modèle du selector | 🔴 | **Innocenté** : 0/20 échecs où il jette la section-réponse. Modèle déjà A/B-é (runs 58/59, non-levier). |
| `min_kept_sections=4` | 🟢 vague 2, **couplé au gate uniquement** | Re-juge Qwen du run 115 : 0,727 vs 0,670, wrong_law 7→3. MAIS amplifie l'hallucination sur les pools vides (cas #200 documenté) → le gate coupe ce canal en amont. Top-up borné aux sections ≥ seuil de gate. min_kept **par ministère** en branche principale (tension SP historique). À requalifier en A/B apparié (le +0,057 est un re-juge non apparié). |
| Plancher « score ≥ 0,7 gardé d'office » (M2) | 🔴 | Réfuté par les distributions : le bruit non-gold score p90 = 0,76 (≈ 2 sections de bruit ≥ 0,7/question) et le gold des passers médiane 0,58. Force-serve du near-miss. |
| Hygiène mécanique (re-tri sortie par score, fallback parse-failure fail-open, header doc_title) | 🟡 | Risque quasi nul mais change le contexte servi → A/B séparé, jamais bundlé. |
| Budget du context_builder trié par score (q28 : 2ᵉ section-réponse jetée au budget) | 🟡 réserve | Cible 1 échec mesuré. |

### 2.6 Côté génération

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| Consignes de prompt (« n'invente pas », « appuie-toi sur les sources ») | 🔴 (comme garantie) | Déjà en place (`generator.py`) et démontrées insuffisantes : le near-miss topiquement adjacent n'est pas reconnu comme « sources insuffisantes » (cas #200 : délai de 4 jours et prud'hommes inventés depuis un contexte hors-sujet). Principe retenu : **aucune garantie par prompt**. |
| **Multi-turn génération vérifiée** (draft → citation de spans exacts → révision) | 🔵 sonde en cours | Prototype du « Programme B ». Sur les 8 échecs génération + 10 contrôles, verdicts appariés (réponse stockée / re-gen single-shot / boucle) ; métrique clé : **taux de spans inventés** (citations fabriquées détectées par string-matching = mesure directe du biais de complaisance dans l'auto-vérification). Critères GO : ≥4/8 conversions réelles, 0 contrôle cassé, spans inventés ~0. |
| Vérification mécanique de citations en prod (chaque affirmation normative ancrée à un span servi, checker externe par string-matching) | 🟡 programme B | La seule garantie « tout est sourcé » **prouvable**. Design conditionné aux résultats de la sonde ci-dessus. |
| A/B du modèle générateur (gpt-oss-120b vs alternatives Albert / qwen ZDR) | 🟡 décision politique | Les 8 échecs génération incluent 4 mésinterprétations juridiques pures = plafond du modèle actuel. L'A/B est techniquement prêt ; l'arbitrage souveraineté (Albert par défaut) est un choix produit, pas technique. Biais à gérer : ne pas juger qwen par qwen. |

### 2.7 Côté mesure (le socle de tout le reste)

| Stratégie | Statut | Preuve / décision |
|---|---|---|
| **Sélection du juge par la mesure (21-22/07)** | ✅ | Qualification de 5 juges par bruit propre (réponses figées jugées 2x). Verdict : **protocole à 2 étages** — OFFICIEL (gates d'adoption) = **Scaleway qwen3-235b souverain en vote majoritaire à 3** (5,1 % single-shot → ~0,8 % maj-3, accord 86,9 % avec l'historique, PR #333) ; SCREENING = grok-4.5 single-shot (ZDR, 6,1 %). Écartés : qwen3.7-max (aucun endpoint ZDR), gemini-2.5-pro (7,1 %), gpt-5.2 (9,1 % + sur-strict 0,35). **Référence officielle = 0,677**, 26 échecs stables = cibles officielles (détail au journal). |
| Bascule de juge Claude → Qwen 3.7 Max (16/07, dépassée le 22/07) | ✅ historique | Claude sur-strict (faux négatifs contredisant sa propre rationale) ; GLM 5.2 incohérent ; Muse Spark géo-bloqué. Qwen : fiable sur les rationales et −71 % de coût, mais adopté sous l'hypothèse désormais invalidée que `data_collection=deny` garantissait la ZDR (PR #324). |
| **Curation goldset** (36 questions) | ✅ | 5 sources fausses re-résolues ; **82 refs LEGIARTI de version re-keyées en cids chroniques sur 28 questions** (dette migration #289) ; 3 questions d'articles abrogés requalifiées CGFP (R331-7/R331-6, L121-6/L121-7, R331-2). Vérifié au run 118 : hit=1.0 sur les curées, +5 conversions. ⚠️ Appliquée en DB staging — à versionner au repo (seed/script). |
| **Protocole de variance — référence historique Qwen** | ✅ historique, dépassé le 22/07 | Découverte majeure du run 118 : **σ single-shot ≈ ±0,05-0,06** (12 flips aléatoires entre deux runs à config identique, hit_rate=1.0 des deux côtés). Un run unique ne peut PAS mesurer +0,02-0,05 — éclaire rétroactivement la « régression min_kept » du 06/07 et le « SP −0,14 » du run 115 (bruit). Protocole durable : verdict majoritaire par question, A/B en **diff apparié par question**, goldset gelé. Sous l'ancien juge Qwen, les runs 118/123/124 donnaient 24 échecs stables historiques, dont q220/q229 ; **ils ne constituent plus les cibles officielles**. La référence officielle Scaleway maj-3 du 22/07 compte 26 échecs stables et reclasse notamment q220/q229 comme instables (ligne ci-dessus, détail au journal). |
| Goldset 300-500 questions (miné des 775 feedbacks testeurs + Grist Suivi-Tests) + juge continu sur trafic réel | 🟡 programme C | 99 questions à ±0,05 de bruit ne peuvent pas piloter une amélioration fine. Les thèmes douloureux réels (typologie_contrats : 48 % négatif) doivent entrer dans la mesure. |

---

## 3. Registre des tests effectués

| Date | Test | Méthode | Verdict |
|---|---|---|---|
| 15-16/07 | Comparatif de juges (Claude, GLM 5.2, Qwen 3.7 Max, Muse Spark, Grok 4.5) | Re-juge des 99 réponses du run 113 + spot-checks croisés | Qwen adopté (runs 116) |
| 16/07 | A/B min_kept 0↔4 | Runs 115/116 + re-juge apparié sous Qwen | +0,057 (à requalifier : non apparié, σ découvert depuis) |
| 17/07 | **Investigation funnel** (workflow 12 agents + panel adversarial 3 lentilles) | Re-jeu offline des 34 échecs : matrice retrieval 7 variantes × top-200, autopsie rerank/selector (vrai reranker Albert rejoué, containment LLM), récupération bout-en-bout | Funnel §1 ; multi-query/RRF pondéré/hybrid/top_k réfutés ; input_k 40 et gate validés |
| 17/07 | **Sonde agentique** (RAG-as-tool multi-turn recherche) | Boucle ReAct 6 appels, 18 questions, 2 modèles (qwen plafond / Albert prod), vérification adversariale des logs | 🔴 NO-GO (1/7 et 0/7 ; `found=true` mensonger 7/7) |
| 17/07 | **Investigation gaps d'ingestion** | Vérif exhaustive manifest PISTE ↔ DB, appels `consult/lawDecree`, diff cache↔corpus | 0 trou ; « manquants » = abrogés 2025 + ids de version pré-#289 |
| 17/07 | **Curation goldset** + re-juge des 2 items 429 | Re-résolution, re-key alias→cid, requalification CGFP ; re-juge offline | q228 PASS (artefact), q660 FAIL réel ; 36 questions corrigées |
| 17/07 | **Rebaseline** (run 118) | Config idem 116, goldset curé | Curation validée (hit=1.0) ; **σ ±0,05-0,06 démontré** (12 flips à config identique) |
| 17/07 | **Sonde graphe de renvois** | Extraction PISTE∪regex, expansion 1 saut bidirectionnelle sur les top-30 des 34 échecs | 🟢 GO (6/34 plafonné, 9/34 max) |
| 17/07 | **Sonde couche de représentation** | 174 enrichissements Albert (aveugles aux questions), 6 variantes, rangs simulés, contrôle différentiel | 🟢 GO ciblé R2 (5/7 misses profonds) ; V1/V3/V4 NO-GO ; R5 réserve |
| 21/07 | **Sonde multi-turn génération vérifiée** | Draft→spans→révision sur les 8 échecs génération + 10 contrôles, verdicts appariés, spans contrôlés par string-matching | 🔵 en cours |

---

## 4. Roadmap consolidée

1. **Vague 0 — stabilisation de la mesure** *(bloquant)* : 2 runs baseline supplémentaires → référence majoritaire-à-3 + σ documenté.
2. **Vague 1 — mécanique runtime** : `v3_rerank_input_k=40` + fix intent gating (derrière flag) ; hygiène selector en A/B séparé.
3. **Vague 2 — anti-hallucination** : gate d'abstention t=0,20 (+ conditions) + min_kept=4 borné, par ministère.
4. **Vague 3 — enrichissement** *(chantier principal)* : pipeline R2 résumés-articles (dgafp d'abord) + `rag_renvois_edges` + dédup chunking SP. Impact combiné projeté ≈ +0,09 (recouvrement limité à q194/q229).
5. **Programme B — génération vérifiée** : selon verdict de la sonde en cours ; sinon A/B générateur (décision souveraineté).
6. **Programme C — mesure à l'échelle** : goldset 300-500 + juge continu sur trafic réel.

**Trajectoire** : 0,670 (116) → mesure assainie ~0,70-0,73 → vagues 1-2 ~0,75-0,78 → vague 3 ~0,82-0,85 → programmes B/C : **0,85-0,90** + zéro réponse fausse confiante (gate + citations vérifiées).

## 5. Principes actés (à ne pas re-débattre sans donnée nouvelle)

1. **Toute stratégie se sonde offline sur les échecs réels avant d'être implémentée** (les sondes ont tué en quelques heures 6 chantiers qui auraient coûté des semaines).
2. **Aucune garantie par prompt** — le biais de complaisance est mesuré (`found=true` sur 7/7 échecs) ; seules comptent les garanties mécaniques (hit rate, seuils, gates, string-matching).
3. **Les enrichissements trouvent, ils ne disent jamais** — tout texte généré (résumés, questions synthétiques) est une clé d'index ; le générateur ne reçoit que le texte source authentique.
4. **Un chiffre d'éval sans protocole apparié est du bruit** (σ ±0,05 single-shot).
5. **Zéro-rétention stricte obligatoire** pour tout LLM externe : sur OpenRouter, `data_collection=deny` **et** `zdr=true` sont requis ; les gates d'adoption utilisent le juge souverain Scaleway maj-3. Albert reste souverain par défaut en prod.
