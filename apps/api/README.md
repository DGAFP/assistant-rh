# Assistant RH API

Installable FastAPI application that will host the OpenAI-compatible Assistant
RH contract. This first scaffold exposes only the unauthenticated operational
probe `GET /healthz`; it does not initialize the RAG pipeline or any AI provider.

## Run locally

The repository uses Proto to install the pinned Python, uv, and Moon versions.
After installing Proto once, bootstrap the repository tools and start the full
API stack:

```bash
proto install
moon run api:local
curl --fail "http://127.0.0.1:${API_PORT:-8000}/healthz"
```

`api:local` builds the API image, starts the dedicated synthetic PostgreSQL
service, and waits for both healthchecks. A successful probe returns:

```json
{"status":"ok","db":"ok","config_loaded":true}
```

Use `API_PORT` and `API_POSTGRES_PORT` to avoid host-port conflicts with another
worktree. Compose scopes container and volume names to the current worktree.

```bash
API_PORT=8010 API_POSTGRES_PORT=55443 moon run api:local
moon run api:local-logs
moon run api:local-down
```

To delete and recreate only the synthetic API database:

```bash
moon run api:local-reset
moon run api:local
```

The API database is a separate volume initialized exclusively from
`tests/fixtures/runtime.sql`. It must never be seeded from staging or production.

The equivalent direct Compose command is:

```bash
docker compose --project-directory . -f docker/api/compose.yml up --build -d --wait
```

## Verify

```bash
moon run api:lint
moon run api:architecture
moon run api:test
moon run api:docker-build
```
