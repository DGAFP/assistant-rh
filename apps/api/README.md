# Assistant RH API

Installable FastAPI application that will host the OpenAI-compatible Assistant
RH contract. This first scaffold exposes only the unauthenticated operational
probe `GET /healthz`; it does not initialize the RAG pipeline or any AI provider.

## Run locally

Start the dedicated synthetic Postgres database, then export its DSN as the
canonical runtime DSN:

```bash
export API_POSTGRES_PORT="${API_POSTGRES_PORT:-55433}"
export API_SYNTHETIC_POSTGRES_DSN="${API_SYNTHETIC_POSTGRES_DSN:-postgresql://assistant_rh_api:assistant_rh_api@localhost:${API_POSTGRES_PORT}/assistant_rh_api_test?sslmode=disable}"
docker compose -f docker-compose.local.yml up -d --wait api-postgres
export SCW_POSTGRES_DSN="$API_SYNTHETIC_POSTGRES_DSN"
uv run --package assistant-rh-api --group dev assistant-rh-api
curl --fail http://127.0.0.1:8000/healthz
```

Use a different `API_POSTGRES_PORT` when another worktree already exposes the
default port. Compose scopes the container and volume to the current worktree.

The API database is a separate volume initialized exclusively from
`tests/fixtures/runtime.sql`. It must never be seeded from staging or production.

## Verify

```bash
uv run --package assistant-rh-api --group dev ruff check apps/api/src apps/api/tests
uv run --package assistant-rh-api --group dev lint-imports --config apps/api/pyproject.toml --no-cache
uv run --package assistant-rh-api --group dev python -m pytest apps/api/tests -v
docker build -f Dockerfile.api -t assistant-rh-api:local .
```
