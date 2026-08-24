# Confrontation à l'état de l'art (revue du 17/08/2026)

Synthèse de la revue de littérature menée pendant la campagne (recherche web
multi-sources, 2024–2026). Complète la revue générale
[`09_REVUE_ETAT_DE_LART_RAG_2026-06.md`](../../audit/09_REVUE_ETAT_DE_LART_RAG_2026-06.md).

## Ce que le design v4 fait conformément à la littérature

**« Le LLM perçoit, le code décide ».** La séparation entre jugements
per-candidat (LLM) et politique déterministe (code) est la forme recommandée
pour la sélection à enjeu élevé : CRAG (évaluateur + routage fixe,
arXiv 2401.15884), SURE-RAG (2026, arXiv 2605.03534 — jugements par paire +
agrégation déterministe transparente, macro-F1 0,91 contre 0,73 pour un juge
LLM monolithique), et le courant neuro-symbolique (AlphaGeometry, systèmes
réglementés 2026). Aucune publication trouvée ne défend la politique LLM de
bout en bout dans ce contexte ; notre run #186 en est une réplication interne
(le `primary_id` choisi par le LLM perd 4 nets).

**Citation verbatim + vérification programmatique.** La fidélité des
citations LLM est mesurée mauvaise (jusqu'à ~57 % de citations non causales,
arXiv 2412.18004 ; CiteCheck 2502.10881) ; demander la citation améliore en
soi l'ancrage (« According to… », EACL 2024, testé sur du droit) ; le patron
« Deterministic Quoting » (santé, 2024) et l'API Citations d'Anthropic (2025)
valident l'approche en production. **La vérification côté code est
nécessaire, pas optionnelle** — nos mesures le confirment (24/99 extraits de
principaux invalides avant correctifs, 0/99 après).

**Signal de suffisance → génération sélective.** « Sufficient Context »
(Joren et al., ICLR 2025, arXiv 2411.06037) : les modèles frontière
hallucinent plutôt que s'abstenir sur contexte insuffisant ; recommandation =
signal de suffisance alimentant une couche de décision **hors** du
générateur. Notre `answerability` implémente cette architecture — mais nos
runs montrent le corollaire que la littérature annonce aussi : l'instruction
d'abstention au niveau du prompt est **suivie de façon stochastique** (ICLR
2025), donc nuisible quand le signal est mal calibré (déclenchement ~45 %).

**Contraintes dures d'autorité et de temporalité dans le code.** Le RAG
juridique 2026 converge : hiérarchie des normes modélisée délibérément
(ontologie Kelsen, arXiv 2505.00039), validité temporelle « hard constraint »
(QA statutaire, arXiv 2605.23497). Nos `_legal_authority_rank` et
`_section_is_obsolete` sont conformes. Limite connue : la supersession doit
idéalement influencer le **retrieval** (arXiv 2604.14488 : le dense retrieval
manque le texte « contrôlant » et nie son existence dans 39 % des cas) — un
selector post-hoc ne récupère pas un texte abrogatoire non retrouvé.

**Entrée listwise, sortie pointwise, garde-fous conservateurs.** Le biais de
position vit dans le classement listwise (arXiv 2503.03064) ; le pointwise pur
ne voit ni redondance ni complémentarité (kapa.ai 2026 : la pertinence est
contextuelle). Notre forme — voir les 12, émettre des jugements indépendants,
laisser le code ordonner — évite le pire des deux. Le plafond de candidats,
le keep-floor et le repli « conserver en cas d'échec » correspondent aux
pratiques de production (kapa.ai : 96 % de recall à 68 % d'élagage ;
arXiv 2601.01896 : le filtrage du bruit est intrinsèquement imparfait, biaiser
vers la conservation).

## Où le design est en avance (donc sans référence externe)

La **taxonomie de rôles fonctionnels** (fondement juridique + mise en œuvre
interne + barème…) formalisant la complémentarité statut/décret/guide
pratique n'a pas d'équivalent publié. Conséquence : pas de benchmark externe —
sa validation repose entièrement sur notre goldset. Résultat : elle **gagne
sur les corpus étagés (MATTE +4)** et n'aide pas les questions à fait unique.

## Où le design est en retrait de l'état de l'art

1. **Porte de suffisance lexicale** : stemmer FR artisanal + liste d'arrêt
   minimale, là où le front est aux vérificateurs entraînés type NLI
   (SURE-RAG, HALT-RAG). Mesuré non calibré (45 % de déclenchement).
   Rétrogradée en signal observé (mode dark) ; à remplacer, pas à re-seuiler.
2. **Sortie structurée longue en un appel** : 12 évaluations × ~7 champs —
   profil de risque troncature/biais de fin documenté (2408.02442,
   littérature production 2025-26). Mitigé par le plafond et la conservation
   prudente ; la parallélisation en lots réduirait troncature ET latence
   (11 s). L'ordre `evidence` avant `relevance` (raisonnement avant décision)
   a été aligné en v4.1.
3. **Vérification post-génération absente** : la recommandation HALT-RAG de
   l'audit de juin (NLI claim-par-claim entre générateur et utilisateur)
   reste le maillon manquant côté produit — l'answerability pré-génération ne
   s'y substitue pas.

## Références principales

Sufficient Context (arXiv 2411.06037, ICLR 2025) · SURE-RAG (2605.03534) ·
CRAG (2401.15884) · Provence (2501.16214, ICLR 2025) · kapa.ai context
pruning (2026) · According to… (EACL 2024) · Deterministic Quoting (2024) ·
Correctness ≠ Faithfulness (2412.18004) · CiteCheck (2502.10881) · Let Me
Speak Freely (2408.02442) · Judgment Distribution (2503.03064) · Noise
filtering difficulty (2601.01896) · Legal ontology Graph RAG (2505.00039) ·
Controlling Authority Retrieval (2604.14488) · Temporal statutory QA
(2605.23497) · Astute RAG (2410.07176) · HALT-RAG (2509.07475).
