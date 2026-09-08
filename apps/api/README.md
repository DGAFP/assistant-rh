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

## Core contracts and PostgreSQL foundation (B1)

`core/ports.py` defines the initial config, prompt, acronym, clock and ID ports.
Stores are async and return immutable `Snapshot` values with an opaque content
revision and an explicit origin. A missing row is `None`; an unavailable database
raises an application error. Search/auth/run/feedback and provider contracts will
be added with their B2/B3 adapters, rather than introducing unneeded domain types
in this foundation. The legacy RAG runtime is unchanged.

The FastAPI lifespan resolves `SCW_POSTGRES_DSN` at startup, creates one async
`Database`, opens it, and closes it on shutdown. Construction and imports do not
connect. A missing DSN keeps `/healthz` at 503; an invalid configured DSN or failed
initial connection prevents startup. `create_app(database=...)` transfers pool
lifecycle ownership to the app; `create_app(health_probe=...)` supports tests
without PostgreSQL. Direct-core runners own `open()`/`close()` themselves.

`db/dsn.py` accepts an explicit DSN or an explicitly supplied environment mapping,
never reads `.env`, and has no historical provider fallback. Host, database and
user must be present, and libpq service-file target resolution is refused. DSNs
are omitted from settings representations. Secrets still belong in deployment
configuration and must never be included in application snapshots.
Pool startup and every reconnect refuse ambient `PGHOST`, `PGHOSTADDR`, `PGPORT`,
`PGDATABASE`, `PGUSER`, `PGSERVICE` or `PGSERVICEFILE`: remove these from the API
process environment and express the target solely in `SCW_POSTGRES_DSN`.

Pool defaults are 1–4 connections per process, at most 16 waiting callers, a
2-second acquisition/startup timeout and a 10-second statement timeout. Adjust
`DatabaseSettings` in the composition root when sizing worker count/retrieval
concurrency against the database connection budget. `statistics()` exposes pool
counts without connection details. Pool behavior follows the
[psycopg pool API](https://www.psycopg.org/psycopg3/docs/api/pool.html).
Checkout health checks have their own 2-second deadline; expiry or cancellation
closes the connection before cancelling the check and letting the pool replace it.
This prevents psycopg's cancellation handshake from waiting on a lost network
response. The queue and checkout-check
deadlines are separate; the statement timeout is enforced by PostgreSQL.

```python
database = Database(DatabaseSettings(dsn=resolve_dsn(environ=environment)))
await database.open()
try:
    async with database.transaction(read_only=True) as connection:
        # SQL stays in db/. Pass one connection to cooperating repositories.
        cursor = await connection.execute("SELECT config FROM public.rag_config WHERE id = 1")
        row = await cursor.fetchone()
finally:
    await database.close()
```

Each lease is one explicit transaction: success commits; exceptions and
cancellation roll back before returning the connection. Repository methods must
not commit manually or retain the connection. For atomic multi-repository writes,
share the lease; use `connection.transaction()` for an inner savepoint. Session
settings such as `ivfflat.probes` must use `SET LOCAL` inside the transaction.
The read-only flag and statement timeout are transaction-local too.

Driver errors are translated after cleanup to `database_conflict` (constraints,
serialization or deadlock), `database_unavailable` (connection, saturation or
timeout), or `database_failure` (other SQL errors). No automatic write retries are
performed. Connection-worker logs are also scrubbed before pool logging.

`freeze_json` detaches and freezes nested configuration; `content_revision`
hashes canonical JSON (sorted object keys, significant array order/text, no NaN).
These hashes detect content equality, **not** monotonic database revisions or
compare-and-swap tokens. B2 must read content and revision in the same snapshot.

`RevisionCache` is per adapter/DB target and event loop, with a bounded LRU, an
explicit TTL and injected monotonic clock. Include scope/ministry/name in keys.
Loads and invalidations are serialized; invalidate only **after commit**, or clear
the cache explicitly. At TTL expiry, failed loads raise instead of serving stale
data. Old snapshots remain immutable for in-flight requests. Cross-process admin
changes become visible on TTL expiry; cross-store/request snapshot orchestration
and store-specific freshness policies belong to B2/C2/C5.

Run the full foundation suite on the dedicated synthetic database:

```bash
API_POSTGRES_PORT=55453 docker compose --project-name assistant-rh-issue-453 \
  --project-directory . -f docker/api/compose.yml up -d --wait api-postgres
API_SYNTHETIC_POSTGRES_DSN='postgresql://assistant_rh_api:assistant_rh_api@127.0.0.1:55453/assistant_rh_api_test?sslmode=disable' \
  uv run --package assistant-rh-api --group dev python -m pytest apps/api/tests -q
```

The fixture rejects nonlocal targets and any database name other than
`assistant_rh_api_test`. CI supplies this DSN so DB tests cannot be skipped there.

## PostgreSQL runtime repositories (B2, #454)

The adapters take the existing `Database` and implement explicit async core
ports. They are additive: only `/healthz` is wired to HTTP at this stage.

| Adapter | Contract |
| --- | --- |
| `ConfigStore`, `PromptStore`, `AcronymStore` | Fresh immutable snapshots with content revisions; inactive/missing prompts return `None`; DB failures remain errors. No rendering or implicit fallback. |
| `GroupStore`, `SessionStore` | Existing groups and `is_admin` roles; hashed opaque sessions, expiry/revocation and invalidation on legacy password reset. Public catalogue/auth policy is B4. |
| `SearchStore` | Separate raw vector, lexical and heading lanes over logical sources; candidate scores/ranks and deterministic ties. No RRF, weighting, gate, deduplication or final selection. |
| `ContentStore` | Batch documents, sections, legal references and chunks, ordered by stable keys. Legacy Service-Public relations and document metadata remain readable. |
| `ChatRunStore` | INSERT-only finalization of run, ordered served sources and all trace events in one transaction. A duplicate completion ID fails instead of overwriting the original. |
| `FeedbackStore` | Group ownership, current feedback, exact retries without writes, atomic audit/replacement, human annotations preserved and AI analysis reset. Analysis writes reject stale generations. |

`core/runtime.py` contains the input/output values; SQL identifiers and driver
objects stay in `db/`. Callers supply already authorized logical sources,
complete run records and normalized feedback. `CompletionIds` generates
`chatcmpl-` plus a full UUID (41 characters); legacy short IDs remain readable.
`session_hash` on run/feedback writes means an **audit pseudonym supplied by the
composition root**, never the bearer or its authentication digest. Session
authentication uses the separate `Session.token_hash`. B4/D1 supply the HMAC
audit pseudonym and enforce public validation, quotas and session policy.
Ownership follows the group, including after session renewal.

The stores deliberately have a zero cache TTL: each call reads committed data
and computes its revision from the same result. They do not hold stale snapshots
after admin changes. Cross-store request snapshots and packaged prompt fallback
are composed in C2/C5; B1's optional cache remains available at that boundary.
Search identifiers are allowlisted; query/vector/limit values are bound. Vector
probes are transaction-local. An absent legacy tsvector is reconstructed from
chunk text for an explicitly requested lexical lane. Lexical queries retain the
historical French OR semantics. Core C3 must still select lanes and prove replay
parity. Returned metadata includes historical role/theme/legal references and
canonical document title/URL even when no section can be resolved.

The versioned migration `20260908094542_api_runtime_repositories.sql` requires
the provisioned historical runtime schema and runs transactionally. It widens
completion IDs, adds sessions, canonical served sources and feedback audit, then
archives all older duplicates before enforcing one feedback per non-null turn.
Ties use `(ts DESC NULLS LAST, id DESC)`. A compatibility trigger accepts legacy
Streamlit INSERTs, preserves the latest submission and human annotations, and
archives replacements. The current row retains its original ID; a separate last
submission ID prevents out-of-order inserts at equal timestamps from winning.
No runtime repository executes DDL. Apply migrations using the normal deployment
workflow; this implementation has only been applied to a local synthetic DB.
Deployment credentials/grants for an audit writer without UPDATE/DELETE remain
part of the runtime-role provisioning before exposing D1.

Repository tests initialize `tests/fixtures/repositories.sql` plus the actual
trace and B2 migrations on the guarded synthetic target. They cover every
repository, all seven logical sources, ordering ties, missing data, database
unavailability, ownership, concurrency, conflicts, rollback, legacy schemas,
lossless deduplication and migration replay. The additional baseline contains
only invented content and three-dimensional vectors, never a database dump.

```bash
API_SYNTHETIC_POSTGRES_DSN='postgresql://assistant_rh_api:assistant_rh_api@127.0.0.1:55433/assistant_rh_api_test?sslmode=disable' \
  uv run --no-sync python -m pytest apps/api/tests -q
```
