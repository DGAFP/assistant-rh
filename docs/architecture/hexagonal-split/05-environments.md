# Environnements du chantier

> Référence : [décisions D10 et D14](06-decisions.md). Avant le premier déploiement Scaleway, le chantier utilise exactement deux environnements : local et VM homelab.

## Avant déploiement : deux environnements

### 1. Local

Usage : développement quotidien, TDD, contrats DB/HTTP et conformance déterministe.

- Postgres + pgvector local dans `docker-compose.local.yml`.
- Corpus seedé par liste blanche selon le runbook existant.
- Schéma runtime créé par les migrations et rempli de données synthétiques : config, prompts, acronymes, groupes/rôles/tokens factices, runs, traces et feedback.
- API locale avec hot reload.
- Fakes/replays pour LLM, embeddings et reranker lorsque l'égalité exacte est attendue.
- SDK OpenAI et instance `conversations` locale pour le contrat client.
- Aucun identifiant, feedback, token ou secret réel copié depuis staging.

```bash
docker compose -f docker-compose.local.yml up -d postgres api
```

La CI reproduit ce niveau : DB vierge, migrations, fixtures synthétiques et tests sans dépendre d'un dump manuel de staging.

### 2. VM homelab

Usage : intégrations longues et réalistes avant tout déploiement cloud.

- Le vrai container API y est lancé avec la même image que celle destinée à Scaleway.
- La VM accède à la DB staging lorsque le réseau développeur ne le permet pas.
- Les providers Albert/Scaleway réels sont utilisés pour les goldsets live ; les replays restent utilisés pour la conformance exacte.
- Le SDK OpenAI, `conversations` et le client Streamlit sous flag y exécutent les scénarios bout en bout.
- Le SSE est testé avec retrieval lent, pings, concurrence, erreur après headers et déconnexion. Cela valide notre serveur ; le comportement spécifique du proxy Scaleway attend staging.
- Toutes les écritures de test portent `source=api-vm` ou un identifiant de run équivalent pour ne pas polluer les statistiques produit.

## Premier déploiement : Scaleway staging dark

L'étape D4 du plan est la première création de l'API sur Scaleway. Ce n'est pas un troisième environnement de développement ou un smoke éphémère : c'est le futur service staging, encore sans trafic Streamlit par défaut.

Il valide ce qui ne peut pas être prouvé localement ou sur la VM :

- build et démarrage du container dans la plateforme cible ;
- cold start, mémoire et limites de concurrence ;
- buffering du proxy, pings SSE, timeout d'idle et durée maximale ;
- annulation/déconnexion à travers le proxy ;
- secrets et accès DB/provider de l'environnement staging ;
- métriques de latence/TTFT, erreurs, connexions DB, fallback provider et persistance.

Seuls les smoke tests, le runner via-API et les clients explicitement autorisés utilisent l'API dark.

## Canary Streamlit staging

- `RAG_CHAT_BACKEND=direct|api` pilote le chat ; le défaut reste `direct` à l'arrivée du code dual-path.
- Les pages admin migrées conservent le même repli jusqu'au nettoyage.
- `api` est activé pour un groupe borné, puis élargi selon les observations.
- Un exercice obligatoire revient à `direct` sans rebuild et vérifie la cohérence des logs/feedback.

## Production et nettoyage

1. Déployer API + Streamlit dual-path avec `direct` encore actif.
2. Exécuter le smoke de l'API dark production.
3. Activer `api` par configuration.
4. Conserver `direct` comme rollback pendant la fenêtre convenue.
5. Supprimer l'ancien chemin dans une promotion ultérieure ; seulement alors retirer le DSN et les autres secrets DB du container Streamlit.

## Matrice récapitulative

| Étape | Environnement/cible | Données | Trafic | Sortie attendue |
|---|---|---|---|---|
| Construction | Local | Corpus seedé + runtime synthétique | Développeurs/CI | Tests rapides et reproductibles |
| Intégration pré-déploiement | VM homelab | Staging, runs tagués | Runners et clients de test | Parité, providers réels, container complet |
| Premier déploiement | Scaleway staging dark | Staging, runs tagués | Tests autorisés uniquement | Contraintes réelles de plateforme validées |
| Canary | Scaleway staging | Staging | Streamlit sous flag | Parité produit + rollback |
| Bascule | Scaleway production | Production | Dual-path puis API | Stabilité avant nettoyage |
