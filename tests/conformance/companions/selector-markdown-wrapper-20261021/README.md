# Transition sélecteur — balise Markdown non fermée

L’archive C5 du 14 septembre 2026 reste inchangée, ainsi que ses empreintes.
Son cas `rag-acronym-contract` contient un JSON complet avec `selected_ids: [0]`
et une balise d’ouverture Markdown sans clôture. L’ancien parseur utilisait
les cinq premiers candidats et perdait la raison du choix. Le parseur corrigé
sert la section 0 et conserve la raison explicite du modèle.

`transition.json` déclare l’unique différence attendue, avec la projection
complète des sections, décisions et raison. Il fixe l’empreinte du manifeste
historique et du fichier de ce cas. Les trois autres sélections, les appels
modèle, prompts et sorties de génération restent comparés exactement à l’archive.
La référence de transition doit être passée explicitement au rejeu ; sans elle,
la comparaison historique stricte continue de signaler cette dérive intentionnelle.
Toute attente non consommée, mauvaise empreinte ou autre modification échoue.

Ce companion teste les étapes C5 avec leurs entrées enregistrées indépendantes.
Il ne prétend pas que la génération enregistrée sous l’ancien contexte serait
celle du pipeline après changement de sélection. L’effet sur les contextes et
la qualité est mesuré séparément sur les cinq sorties réelles concernées, avec
le ContextBuilder courant et des générations Albert répétées.
