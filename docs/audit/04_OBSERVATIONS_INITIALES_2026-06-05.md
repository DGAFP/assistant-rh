# Observations initiales (Day 1) — réconciliées

> Dossier d'audit : voir [README](README.md).
> Note d'origine : **2026-06-05**, fin de journée 1 (onboarding Noellie, priorités fonctionnelles Valentin, export eprod, audit rapide du repo). Auteur : Paul, avec relecture de l'agent Lucie.
> Réconciliation : **2026-06-09**, à la lumière des notes [01](01_RAG_QUALITY_AUDIT_2026-06.md) / [02](02_ARCHITECTURE_AUDIT_2026-06.md) / [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md).

## Pourquoi cette note

Cette note conserve la **vision stratégique de Day 1** : elle a posé, avant tout accès aux données, le bon point de pression (distinguer les familles d'échec avant d'optimiser) et un périmètre large (scope multi-ministère, habilitations, taxonomie d'erreurs, calibration de l'éval, axes produit) que les audits ultérieurs n'ont pas tous recouverts.

**Lentille de lecture** : c'étaient des observations de J1, beaucoup à l'état d'hypothèse. Les sections ci-dessous marquent explicitement ce qui est depuis **✅ confirmé**, **◐ nuancé / à requalifier**, ou **🆕 apport encore ouvert** non traité par les notes 01-03. Les hypothèses J1 invalidées sont signalées comme telles plutôt que supprimées, pour garder la trace du raisonnement.

---

## 1. Ce qui est confirmé depuis (renvoi notes 01-03)

| Observation J1 | Statut | Référence |
|---|---|---|
| Delta auto-éval (notes hautes) vs jugement expert — « point qui conditionne le reste » | ✅ **Confirmé et caractérisé** : disparité structurelle (population exclue, métriques conditionnées au contexte et non à la vérité terrain, juges non calibrés, goldset vide) | Note 01, addendum 2 |
| Échecs 1-2★ = surtout documents manquants / mauvais chunks / réponses incomplètes, **pas d'abord de la génération** | ✅ **Confirmé** : 58 % des feedbacks négatifs = `retrieval_issue`, 23 % = `missing_document` ; motif n°1 « Incomplet » | Note 01, §1 et §2.3 |
| Chunks de mauvaise qualité (titres tronqués, sections fragmentées, navigation mêlée au contenu) | ✅ **Confirmé** : 33 % de doublons SP, 20-33 % de chunks-titres, sections de 6 à 174 k chars, chunks-titres « ANNEXE 5 » / « Astreintes et permanences » | Note 01, §2.2 |
| Audit chunks = prérequis avant tout tuning RRF | ✅ **Confirmé et étendu** : le vrai blocage est en amont du RRF — 58 % des fiches SP n'ont **aucun chunk** (trou de couverture index, cas SFT) | Note 01, addendum 1 |
| Besoin d'abstention / refus quand la base ne contient pas l'info | ✅ **Confirmé** : le selector LLM est aujourd'hui le seul garde-fou, sans signal de score fiable (RRF plat) | Note 01, §2.1 et §2.4 |
| Manque d'observabilité (signaux non consolidés) | ✅ **Confirmé** : panne reranker restée invisible des mois ; dossier dédié | Notes 02 §4, 03 |

**Le diagnostic central de Day 1 tient** : les défaillances sont majoritairement en amont (couverture, chunking, retrieval, confusion de domaine, absence de refus), et la calibration de l'éval conditionne la mesure de toute amélioration.

---

## 2. À requalifier (revue Lucie + données ultérieures)

- **◐ Les chiffres de l'export beta sont un signal diagnostique biaisé, pas une mesure produit.** L'export J1 (215 feedbacks : 54,4 % 4-5★ / 33 % 1-2★) est un sous-ensemble auto-sélectionné et porte sur les *étoiles*. La mesure consolidée ultérieure porte sur 761 feedbacks et le flag *helpful* (74 % positifs) — métriques et fenêtres différentes, **non directement comparables**. À lire « parmi les réponses notées… », jamais « le système réussit X % du temps ». Compléter par : nb total de conversations/réponses, taux de feedback, représentativité par thème et profil.
- **◐ « Document manquant » est trop large.** Recouvre ≥ 6 situations distinctes (absent du corpus / non ingéré / ingéré ailleurs / mal chunké / présent mais non retrouvé / filtré à tort / hors périmètre / présent en source juridique mais pas ministérielle). La note 01 (addendum 1) en a déjà tranché une part majeure : beaucoup de « manquant » sont en réalité des **trous d'index** (document présent, sections présentes, zéro chunk). À resserrer cas par cas **avant** de lancer un chantier d'ingestion.
- **◐ La calibration ne doit pas porter que sur les 1-2★.** Pour mesurer si l'auto-éval prédit le jugement expert, inclure des 4-5★, des 3★, et surtout les cas divergents (apprécié utilisateur / invalidé expert, et inversement juridiquement correct mais frustrant). Sinon le juge ne sait reconnaître que les échecs évidents.
- **◐ Scaleway : ce n'est pas une « migration » mais un sujet « environnements & données ».** La cible active est déjà Scaleway (Streamlit Serverless, jobs d'ingestion, PostgreSQL+pgvector). La vraie question : quels environnements, avec quelles données/secrets/droits, et lequel sert de **baseline d'éval reproductible** — ce que la note 01 a confirmé concrètement (écart local/staging : `rag_chunks_test` absente, deux générations d'ingestion).
- **◐ Scope ministériel : séparer 3 niveaux.** (1) **Filtrage d'autorisation** (ce que l'utilisateur a le droit de voir → avant retrieval, en SQL), (2) **politique de priorité des sources** (ce qu'il faut privilégier selon le type de question → ranking/selector/policy), (3) **modèle d'autorité documentaire** (qui prime en cas de contradiction). « RGRH > reste » est trop simple : il faut une hiérarchie contextualisée (source, périmètre, date, type de question, contradiction).

---

## 3. Apports encore ouverts (non couverts par les notes 01-03)

Ces axes restent à instruire — ils dépassent le périmètre des audits qualité/archi/observabilité et structurent l'itération 2 côté produit et sécurité.

**Scope multi-ministère (🆕, structurant).** Le ministère ne doit pas être un champ du prompt mais un attribut serveur (`ministry_id`) croisé avec les claims et **appliqué en SQL avant reranking**. Périmètre type MATTE : sources MATTE + interministérielles + Service-Public + DGAFP + Légifrance + RGRH si pertinent, autres ministères exclus. Prérequis de l'ingestion MI/MSO/MASA/MEF.

**Authentification & habilitations (🆕, structurant).** ProConnect (OIDC), allowlist transitoire, **enforcement du scope dans la retrieval** (pas seulement l'UI), administration déléguée par ministère. Lié à la note 02 §5 (modèle d'autorisation actuel fragile : cookie de groupe) et au sujet RGPD (rétention des conversations).

**Classifier de question + politiques RRF par type (🆕).** Typer la question (procédure ministérielle / réglementaire / rémunération / mobilité / formation / document absent / vague / hors périmètre) puis pondérer les sources : procédure interne → ministériel ; juridique/daté → DGAFP/réglementaire ; donnée de référence → RGRH contrôlante ; pas de couverture → refus/clarification. À valider sur benchmark — et **après** correction du scoring (le RRF plat de la note 01 rend toute pondération illisible aujourd'hui).

**Clarification vs rebond (🆕).** Clarification *avant* génération (peut bloquer) sur question vague / terme ambigu / dispositifs proches / suite mal rattachée ; rebond *après* génération (UX). Ne pas clarifier quand la question est claire (répondre, ou refus propre si non couvert).

**Contrôle de confiance / abstention stricte (🆕).** Pas de réponse complète si : aucun chunk pertinent, sources trop faibles, meilleurs chunks d'un sujet confusable, demande de formulaire absent, contradiction non résolue. Règle de génération : source précise sur une règle (durée/plafond/condition) → interdire le conditionnel, citer la source ; sinon → abstention courte + clarification. Dépend d'un **score de pertinence fiable**, donc du chantier scoring (note 01).

**Modèle d'autorité des sources + RGRH contrôlante (🆕).** Formaliser une hiérarchie contextualisée plutôt qu'une règle fixe ; rendre RGRH visible quand elle contredit/complète une fiche ; signaler sources anciennes/non datées.

**Affichage des sources (🆕, UX prioritaire).** Document lisible (pas le chunk), fiche/article/décret/URL, date, signalement d'ancienneté, ciblage page/section PDF à l'ouverture, signalement de contradiction. Contrôle `citation_completeness` à intégrer à l'éval.

**Prompts & choix du modèle (P2.5).** Hypothèse : prompts system courts, structurés, en anglais avec « Answer in French » ; séparation par étape. Positionnement produit à trancher : **« assistant des gestionnaires RH »** (recommandé) vs « spécialiste RH ». LLM générateur : Mistral medium signalé trop bavard/extrapolant → tester contre alternatives plus contraintes, une fois la baseline figée. Ne pas traduire les termes juridiques.

**Embeddings RH-spécifiques (🆕).** Vérifier que le vocabulaire métier est bien capturé (CET, ASA, ARE, IJ, IFSE, IFC, CMO, CGM, RIFSEEP, RIAA…). Sinon : dictionnaire de synonymes en query expansion ou modèle d'embedding plus adapté. Lié aux confusions récurrentes observées : `IFSE`/`IFC`, `mobilité`/`mobilité durable`, `indemnité de résidence`/`changement de résidence`, `temps partiel`/`temps partiel thérapeutique`, `CMO`/`CGM`.

**Fragilité par thème (🆕, à rafraîchir).** Export J1 — thèmes faibles : Renouvellement/Mobilité (2,22 ; 78 % de 1-2★), Formation (2,50), Rémunération/Paie (3,17), Recrutement (3,33) ; thèmes solides : Temps de travail (3,89), Fin de contrat (3,78), Typologie contrats (3,57), Congés (3,55). À recalculer sur les données courantes et à utiliser pour **stratifier le benchmark** et cibler l'audit chunks.

**Pistes exploratoires (P3, conditionnelles).** Deep research agentique borné par un workflow multi-retrieval observable ; retrieval graph / GraphRAG documentaire orienté entités-relations — **garde-fou** : ne pas démarrer avant que l'audit chunks ait conclu que les échecs viennent de relations manquantes, pas d'un chunking insuffisant (sinon graphe sur du bruit). Upload de document utilisateur limité à la conversation.

---

## 4. Trois livrables immédiats (revue Lucie) — alignés itération 2

1. **Schéma d'erreur RAG partagé** : taxonomie commune (source absente / non ingérée / mal chunkée / chunk non retrouvé / mal classé / source prioritaire absente / selector incomplet / génération incomplète / extrapolation / trop assertive sur contexte faible / clarification / hors périmètre / contradiction-obsolescence / citation insuffisante). Base de l'audit, de l'éval et des futures PR. → Recoupe directement la Phase 1 (Mesure) de la note 01.
2. **Petit benchmark diagnostique figé** : 50-80 cas issus de la beta, stratifiés (note / thème / type d'échec / présence supposée du document / cas sensibles), rejouables avant/après. → C'est le goldset v1 de la note 01, Phase 1.
3. **Audit chunks/sources sur les thèmes les plus fragiles** (Renouvellement/Mobilité, Formation, Rémunération/Paie, Recrutement) : pour chaque cas, dérouler la chaîne document existe ? → ingéré ? → bien chunké ? → contexte parent ? → remonté ? → conservé par le selector ? → exploité par le generator ? → source citée exploitable ? → dit précisément quel est le prochain chantier.

---

## 5. Priorisation Day-1 (conservée, recadrée itération 2)

> Lecture : la priorisation J1 reste valable. La note 01 a depuis **livré un quick win** (fix reranker, [#88](https://github.com/DGAFP/assistant-rh/pull/88)/issue #87) et confirmé que P1.1-P1.2 sont bien les bloquants.

- **P1 — Bloquants pour pouvoir juger** : (1) mesurer l'écart auto-éval/expert et statuer sur l'usage de l'éval comme proxy ; (2) audit qualité chunks par source + typologie ; (3) environnements/données Scaleway disponibles pour une baseline.
- **P1.5 — Structurels en parallèle** : scope ministériel serveur + filtrage SQL ; ingestion MI/MSO/MASA/MEF + métadonnées normalisées ; ProConnect + administration déléguée ; blocage des données personnelles.
- **P2 — Après baseline calibrée** : classifier ; policies RRF par type ; abstention stricte ; clarification avancée ; pop-up thématiques + roadmap visible (attente n°1 utilisateurs).
- **P2.5 — Produit/UX après baseline** : affichage enrichi des sources ; rebond ; support ; API/Mastra observable ; historique ; prompts EN testés ; changement de LLM ; disclaimer + visuel IA ; prototypes deep research / graph bornés.
- **P3 — Exploratoire/conditionnel** : upload utilisateur ; agentic RAG généraliste ; GraphRAG généraliste ; gating CI bloquant tant que le benchmark n'est pas calibré.

---

## Sources

- Note d'observation initiale du 2026-06-05 (Paul) et sa relecture (agent Lucie).
- Export eprod beta du 2026-06-05 (215 réponses avec feedback).
- Réconciliation : notes [01](01_RAG_QUALITY_AUDIT_2026-06.md), [02](02_ARCHITECTURE_AUDIT_2026-06.md), [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) du présent dossier (constats vérifiés sur code + base locale copie staging, 2026-06-09).
