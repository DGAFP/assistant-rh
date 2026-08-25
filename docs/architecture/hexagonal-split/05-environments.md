# Environnements du chantier

> Référence : [00-overview.md](00-overview.md) (décisions D10, D14). L'API est validée et déployée à côté de l'existant avant que Streamlit ne la consomme.

## 1. Dev quotidien — full local

Extension de l'environnement local (`docs/LOCAL_DEV.md`) :

- **DB corpus/éval** : Postgres pgvector docker `:55432`, seedé par liste blanche depuis staging selon le runbook existant.
- **DB runtime synthétique** : migrations et fixtures locales pour `rag_config`, prompts, acronymes, groupes/tokens factices, `chat_runs`, traces et feedback. Aucun `chat_*`, identifiant, feedback ou secret réel n'est copié depuis staging.
- **API** : service `api` ajouté à `docker-compose.local.yml` (uvicorn, hot-reload), branché sur la DB locale.
- **LLM** : Albert par clé API quand la fidélité live compte ; replays/fakes pour la conformance exacte ; modèle local autorisé pour les boucles rapides.
- **Clients** : SDK OpenAI, client Streamlit sous flag et instance `conversations` de spike.

```bash
docker compose -f docker-compose.local.yml up -d postgres api
```

La CI démarre une DB vierge, applique les migrations et charge uniquement les fixtures synthétiques. Un test qui exige un dump manuel de staging n'est pas un test de contrat bloquant.

## 2. Spikes de phase 0

### Compatibilité `conversations`

- API minimale locale ou sur VM homelab, données factices/replay.
- Validation du backend `conversations` vers l'API : auth, `/v1/models`, mapping messages, stream, erreurs, sources markdown et feedback.
- Les tests reproductibles issus du spike restent dans le repo et deviennent des gardes C7.

### SSE Scaleway précoce

- Workflow `workflow_dispatch` sur un container éphémère minimal.
- Retrieval simulé suffisamment long pour observer pings, buffering proxy, timeout d'idle, durée max, déconnexion et erreur après envoi des headers.
- Extinction à la fin du spike. Un échec peut modifier C3 avant que le moteur ou Streamlit ne dépende du choix technique.

## 3. Parité moteur — VM homelab

- `ssh dev@assistant-rh` (repo + `.env` en place ; la VM atteint la DB Scaleway quand le réseau pro la bloque).
- Nouveau core et ancien runtime exécutés avec la même révision de config et le même snapshot corpus staging.
- **Conformance exacte** avec ports/replays figés ; **qualité live** avec appels providers séparés et tolérances consignées.
- Les écritures de test portent `source=api-vm` ou un tag de run équivalent afin de ne pas polluer les stats produit.

## 4. API dark staging

- Après C7, le vrai `Dockerfile.api` est déployé sur Scaleway staging à côté du Streamlit existant.
- Aucun trafic Streamlit par défaut : seuls smoke tests, runner via-API et clients explicitement autorisés l'utilisent.
- Min-scale initial choisi selon le coût et les besoins du canary ; la décision finale de min-scale est prise sur mesures de cold start/latence.
- Secrets : DSN staging, clés providers, `ADMIN_TOKEN`, matériel d'auth groupe de test. Les tokens ne sont jamais injectés dans du code client navigateur.
- Observabilité obligatoire avant D1 : taux d'erreur, latence/TTFT, annulations, connexions DB, fallbacks provider, échecs de persistance et source des runs.

## 5. Canary Streamlit staging

- `RAG_CHAT_BACKEND=direct|api` pilote le chat ; défaut `direct` à l'arrivée du code dual-path.
- Les pages admin migrées disposent du même mécanisme de repli jusqu'à l'arbitrage de nettoyage.
- Activation `api` pour un groupe/canary borné, puis élargissement progressif selon les observations du canary.
- Exercice obligatoire : retour à `direct` sans rebuild, puis preuve que logs/feedback restent cohérents.

## 6. Production et nettoyage

1. Déployer API + Streamlit dual-path avec `direct` encore actif.
2. Smoke API dark production.
3. Activer `api` par configuration et observer pendant la fenêtre convenue.
4. Conserver `direct` comme rollback jusqu'à validation explicite.
5. Supprimer l'ancien chemin dans une promotion ultérieure ; seulement alors retirer DSN/secrets DB du container Streamlit.

## Matrice récapitulative

| Environnement | Quand | DB/données | Trafic | Sortie attendue |
|---|---|---|---|---|
| Local compose | Tous les jours | Corpus seedé + runtime synthétique | Développeurs/tests | Tests reproductibles sans données personnelles |
| Spike `conversations` | Phase 0 | Fakes/replay ou local | Client réel de test | Contrat OpenAI consommable |
| Smoke SSE Scaleway | Phase 0 | Aucune ou factice | Script de smoke | Architecture streaming validée tôt |
| VM homelab | M1/M2, puis contrôle avant M3 si nécessaire | Staging, runs tagués | Runners de parité | Conformance exacte + qualité live |
| API dark staging | Phase C | Staging, runs tagués | Tests autorisés uniquement | Opérabilité avant consommateur |
| Canary staging | Phase D | Staging | Streamlit sous flag | Parité produit + rollback testé |
| Production dual-path | Phase E | Production | Bascule progressive | Stabilité avant nettoyage |
