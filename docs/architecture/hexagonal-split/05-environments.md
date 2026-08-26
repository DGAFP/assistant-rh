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

En D4, le service API staging est déployé sans trafic Streamlit par défaut. Aucun environnement cloud temporaire supplémentaire.

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

La [phase F du plan](04-migration-plan.md#phase-f--production-stabilité-puis-nettoyage) prévoit le smoke production, l'activation API et une fenêtre de rollback vers `direct`.

Le DSN et les secrets DB de Streamlit ne sont retirés qu'avec l'ancien chemin, dans une promotion ultérieure après stabilité.
