# Scaleway PostgreSQL et job d'ingestion

## Choix de service

Pour reproduire le comportement de la base Scalingo sur Scaleway, il faut utiliser **Managed Databases for PostgreSQL and MySQL** et non Serverless SQL :

- compatibilité PostgreSQL plus proche d'une instance managée classique
- gestion de bases logiques via API/console/SQL
- support de `pgvector`
- extensions utiles déjà disponibles : `pgvector`, `pgcrypto`, `pg_trgm`, `unaccent`, `btree_gin`

Sources officielles :

- https://www.scaleway.com/en/managed-postgresql-mysql/
- https://www.scaleway.com/en/docs/managed-databases-for-postgresql-and-mysql/reference-content/postgresql-extensions/
- https://www.scaleway.com/en/developers/api/managed-databases-for-postgresql-and-mysql/
- https://www.scaleway.com/en/pricing/managed-databases/

## Dimensionnement recommandé

Pour une cible "similaire à Scalingo" avec l'app Streamlit, le retriever pgvector et un job d'ingestion mensuel, la recommandation pragmatique est :

- `PostgreSQL-17`
- `DB-PLAY2-NANO`
- `fr-par`
- stockage `sbs_5k` 20 GB
- pas de HA dans un premier temps

Pourquoi :

- `DB-DEV-S` est moins cher mais trop serré pour un usage applicatif + vector search en prod
- `DB-PLAY2-NANO` donne 2 vCPU / 4 GB RAM et reste dans un coût raisonnable
- `PostgreSQL-17` est bien disponible et aligne le setup sur l'offre actuelle Scaleway

## Coût mensuel estimatif

Estimation au 1 avril 2026, hors taxes, avec 730 heures par mois :

- `DB-PLAY2-NANO` : `0.0432 €/h` soit environ `31.54 €/mois`
- `sbs_5k 20 GB` : `0.0993 €/GB/mois` soit environ `1.99 €/mois`
- `backups 20 GB` : `0.03 €/GB/mois` soit environ `0.60 €/mois`

Total estimatif :

- **environ `34.13 €/mois` HT**

Option plus agressive coût minimal :

- `DB-DEV-S` : `0.0156 €/h` soit environ `11.39 €/mois`
- `sbs_5k 20 GB` : `1.99 €/mois`
- `backups 20 GB` : `0.60 €/mois`
- total : **environ `13.98 €/mois` HT**

## Bootstrap du schéma

Le schéma SQL cible est ici :

- [scaleway_postgres_core_schema.sql](../../config/sql/scaleway_postgres_core_schema.sql)

- `rag_chunks_service_public`

Dans la phase actuelle de migration, il crée volontairement **seulement** la table cible utile au chargement Service-Public :

- `rag_chunks_service_public`

## Script d'application du schéma

Le bootstrap SQL s'applique avec :

```bash
python3 scripts/bootstrap_scaleway_postgres.py
```

Le script lit par défaut :

- `SCW_POSTGRES_DSN`

Tu peux aussi passer un DSN explicitement :

```bash
python3 scripts/bootstrap_scaleway_postgres.py --dsn 'postgresql://...'
```

## Job d'ingestion

Le job d'ingestion Service-Public est ici :

- [service_public_ingestion_job.py](../../scripts/service_public_ingestion_job.py)

Image dédiée :

- [Dockerfile.service_public_ingestion](../../Dockerfile.service_public_ingestion)

Manifest de référence :

- [scaleway_serverless_job_service_public_ingestion.json](../../config/scaleway_serverless_job_service_public_ingestion.json)

Il reprend l'idée du notebook `ingestion_pdf.ipynb` :

- lecture des artefacts JSONL
- transformation des embeddings vers le format `pgvector`
- UPSERT par clé stable
- cible `rag_chunks_service_public`

### Mode local

```bash
python3 scripts/service_public_ingestion_job.py \
  --lake-root data/lake/service_public \
  --target-env prod
```

### Mode Object Storage -> Postgres

```bash
python3 scripts/service_public_ingestion_job.py \
  --lake-root data/lake/service_public_ingest \
  --target-env prod \
  --from-object-storage
```

Le script télécharge alors `silver` et `gold` depuis :

- `assistant-rh-gold/prod/gold/service_public/...`

## Pourquoi ce job est spécifique à Service-Public

Pour l'instant, ce job est volontairement **spécifique à Service-Public**.

Pourquoi :

- la migration en cours porte d'abord sur cette source
- le format des artefacts `silver/gold` et la cible SQL sont déjà stabilisés pour cette source
- cela évite de sur-abstraire trop tôt un orchestrateur "générique" multi-sources

Ce que ça veut dire :

- `job 1` Service-Public produit `bronze/silver/gold`
- `job 2` Service-Public lit les artefacts `gold` et charge `rag_chunks_service_public`

Plus tard, si tu ajoutes MATTE, RGRH ou Legifrance dans la même logique, on pourra sortir un worker d'ingestion plus générique avec :

- une interface commune par source
- une table de `ingestion_runs`
- un mapping source -> tables cibles
- une stratégie d'upsert et de contrôle qualité unifiée

Mais pour maintenant, le choix le plus propre est un job 2 **relié à Service-Public**.

## Déploiement du job 2 sur Scaleway

Build de l'image :

```bash
docker buildx build \
  --platform linux/amd64 \
  -f Dockerfile.service_public_ingestion \
  -t rg.fr-par.scw.cloud/assistant-rh/service-public-ingestion:latest \
  --push \
  .
```

Configuration recommandée du Serverless Job :

- nom : `service-public-ingestion-monthly-prod`
- image : `rg.fr-par.scw.cloud/assistant-rh/service-public-ingestion:latest`
- CPU : `1000 mvCPU`
- mémoire : `2048 MiB`
- stockage local : `2048 MiB`
- timeout : `7200s`
- cron : `30 3 1 * *`

Commande :

```bash
python scripts/service_public_ingestion_job.py \
  --dsn-env SCW_POSTGRES_DSN \
  --from-object-storage \
  --target-env prod
```

## Variables d'environnement

À renseigner :

- `SCW_POSTGRES_DSN`
- `SCW_ACCESS_KEY`
- `SCW_SECRET_KEY`
- `SCW_DEFAULT_REGION=fr-par`
- `SCW_BUCKET_SILVER=assistant-rh-silver`
- `SCW_BUCKET_GOLD=assistant-rh-gold`
- `SCW_PREFIX_PROD=prod`

## Blocage actuel

Instance créée le `3 avril 2026` :

- nom : `assistant-rh-postgres`
- moteur : `PostgreSQL-17`
- node type : `db-play2-nano`
- région : `fr-par`
- volume : `sbs_5k`, `20 GB`
- base logique : `assistant_rh`

Les prochains pas :

1. charger les prompts/config utiles si besoin
2. builder et pousser l'image `service-public-ingestion`
3. créer le Serverless Job d'ingestion mensuel
4. lancer `service_public_ingestion_job.py`
