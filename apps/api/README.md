# Assistant RH API

Installable FastAPI application that will host the OpenAI-compatible Assistant
RH contract. It exposes the operational probe `GET /healthz` and public group
session authentication and the protected `GET /v1/models` catalogue. It does not initialize the RAG pipeline or any AI provider.

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

The API database is a separate volume initialized in one transaction from
`docker/api/bootstrap-local.txt`: synthetic fixtures, the real B2/B4 migrations,
and a public local demo group. It must never be seeded from staging or production.
The initializer only runs on an empty volume; after upgrading an older local
stack, use `api:local-reset` then `api:local` to recreate its synthetic data.

The loopback-only demo account is `local-demo` / `local-only-password`. Verify
catalogue, login, `/me`, `/v1/models`, logout and rejection of the revoked bearer with:

```bash
uv run --package assistant-rh-api python docker/api/smoke-auth.py
```

These public demo credentials are local fixtures, never production credentials.

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

`core/ports/configuration.py` defines config, prompt and acronym ports;
`core/ports/system.py` defines clock and ID ports.
Stores are async and return immutable `Snapshot` values with an opaque content
revision and an explicit origin. A missing row is `None`; an unavailable database
raises an application error. B2/B3 extend these ports alongside their concrete
runtime repositories and inference gateways. The legacy RAG runtime is unchanged.

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

`core/models/` groups input/output values by domain: `configuration`, `auth`,
`retrieval` and `conversations`. The matching `core/ports/` modules define their
operations; consumers import these modules explicitly. SQL identifiers and driver
objects stay in `db/`. Callers supply already authorized logical sources,
complete run records and normalized feedback. `CompletionIds` generates
`chatcmpl-` plus a full UUID (41 characters); legacy short IDs remain readable.
`session_hash` on run/feedback writes means an **audit pseudonym supplied by the
composition root**, never the bearer or its authentication digest. Session
authentication uses the separate `Session.token_hash`. B4/D1 supply the HMAC
audit pseudonym and enforce public validation, quotas and session policy.
Ownership follows the group, including after session renewal.
Feedback reasons are immutable tuples in the core (JSON arrays at the HTTP
boundary). Only the PostgreSQL adapter joins/splits the legacy `; `-separated
TEXT columns; empty or NULL stored reasons become empty tuples.

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
The legacy analyzer captures the feedback revision before its LLM work and
conditionally saves only that still-current, unanalyzed generation. The query
also works before the revision column exists. Rollout must replace/drain old
analyzer workers before enabling the replacement trigger; those old workers
still write by ID alone. API feedback writers use `FOR NO KEY UPDATE` on the
parent so legacy INSERTs can finish their foreign-key `KEY SHARE` checks while
holding the advisory lock, without a parent/advisory deadlock.
No runtime repository executes DDL. Apply migrations using the normal deployment
workflow; this implementation has only been applied to a local synthetic DB.
Deployment credentials/grants for an audit writer without UPDATE/DELETE remain
part of the runtime-role provisioning before exposing D1.

Repository tests initialize `tests/fixtures/repositories.sql` plus the actual
trace and B2 migrations on the guarded synthetic target. They cover every
repository, all seven logical sources, ordering ties, missing data, database
unavailability, ownership, concurrency, conflicts, rollback, legacy schemas,
lossless deduplication and migration replay. Coexistence tests exercise the real
legacy analyzer across API/Streamlit replacements (including A → B → A), the
pre-migration schema, and concurrent first INSERTs with the historical foreign
key installed. The additional baseline contains
only invented content and three-dimensional vectors, never a database dump.

```bash
API_SYNTHETIC_POSTGRES_DSN='postgresql://assistant_rh_api:assistant_rh_api@127.0.0.1:55433/assistant_rh_api_test?sslmode=disable' \
  uv run --no-sync python -m pytest apps/api/tests -q
```

## Inference gateways (B3, #455)

`core/models/inference.py` and `core/ports/inference.py` define immutable inference values and
`LLMPort`, `EmbeddingPort`, `RerankerPort`. `gateways/` implements them through
an injected `httpx.AsyncClient`; the core imports neither HTTPX nor provider SDKs.
Construction reads no environment, creates no singleton and performs no I/O.
The composition root owns the client lifecycle and supplies explicit HTTPS base
URLs (including `/v1`), keys, deployed model names and request budgets. Endpoint
representations and errors omit credentials, URLs, prompts and provider bodies.
Use a dedicated client with `trust_env=False` unless the deployment explicitly
requires a proxy. Redirects are disabled even if the injected client enables them.

| Adapter | Success | Failure / fallback |
| --- | --- | --- |
| `ChatGateway.complete` | Trimmed text, provider, requested model, finish reason, immutable attempt history. | Albert then optional Scaleway; double failure raises `InferenceFailure`. A single configured endpoint supports primary-only calls. |
| `ChatGateway.stream` | Nonempty `TextDelta` events, then one `StreamCompleted` with provider/model/attempts. | Retry/fallback only before content; after content, raise `InferenceFailure(partial=True)`. Never splice a second model into a partial answer. EOF without `[DONE]` is invalid. |
| `EmbeddingGateway` | L2-normalized vector **with its logical model key**: `albert` / 1024 dimensions or `bge_scaleway` / 3584 dimensions. | Albert then optional Scaleway; double failure raises `InferenceFailure`, so C3 can deliberately choose lexical retrieval. Never infer SQL columns from shared state. |
| `RerankerGateway` | Batches of 40, all candidates scored before global `(-score, original_index)` ordering and `top_k`. | Any failed batch discards partial ranking; default result explicitly marks input-order fallback with historical `1 - i * .001` scores. `fallback_on_error=False` raises instead for callers preserving existing input scores. |

Every actual HTTP attempt records provider/model and an optional stable failure
kind: `timeout`, `unavailable`, `rate_limited`, `rejected`, `invalid_response`.
`InferenceFailure` lives in `core/errors/inference.py`. Its `attempts` attribute contains the
same safe values; it never wraps a raw provider exception for display.
No result, trace or `last_*` diagnostic is shared
between requests. Cancellation propagates and does not initiate fallback or
open the embedding circuit. Response cleanup is bounded and shielded against
ASGI/AnyIO cancellation and repeated asyncio task cancellation, including HTTPX's
automatic close at EOF. Cancellation waits for the underlying cleanup task to
finish or exhaust its I/O budget, even after HTTPX marks the response closed.
Early stream consumers must exit the context:

```python
async with httpx.AsyncClient(trust_env=False) as client:
    gateway = ChatGateway(client, albert_endpoint, scaleway_endpoint)
    request = ChatRequest((Message("system", system_prompt), Message("user", query)))
    async with gateway.stream(request) as events:
        async for event in events:
            # Render TextDelta; store StreamCompleted on this request's context.
            consume(event)
```

This follows HTTPX's [async lifecycle](https://www.python-httpx.org/async/).
The stream deadline is checked during reads and buffered SSE processing; it
does not install a timeout across `yield` that could cancel consumer work.

`RequestPolicy` defaults to 10 s per I/O operation, 30 s total **per provider**,
two attempts (configurable 1–3), fixed 100 ms retry delay and an 8 MiB response
limit. Chat uses 120 s I/O / 240 s total per provider by default. Total budgets
include retries and, for reranking, all batches; fallback has a fresh provider
budget. Cleanup has at most one additional I/O timeout per opened response.
Only network errors, timeouts, 429 and 5xx retry; other HTTP statuses and invalid
payloads go directly to the configured fallback. Arbitrary `Retry-After` values
cannot extend the configured budget. Inject tighter deployment budgets as needed.

The optional `EmbeddingCircuit(clock)` reproduces the historical 60 s Albert
cooldown. It belongs to a gateway/target in the composition root, uses an injected
monotonic clock and a lock only around its small technical state. A stale success
cannot reset a newer failure. Calls skipped during cooldown record `circuit_open`.
Expiry admits concurrent primary calls, as in the legacy cooldown. The circuit
does not retain vectors, model selection or request diagnostics.

Explicit differences to carry into extraction C2–C7: empty stream deltas do not
prevent fallback; malformed/truncated streams are errors; zero/nonfinite/wrong-size
vectors are rejected; reranker zero scores remain zero rather than selecting an
alternate score field. Strict provider response validation and bounded retries
replace permissive SDK/legacy behavior. Partial-error rendering and lexical/
input-ranking fallback policy stay with the core/handlers. The composition root
must select embedding deployments compatible with existing indexed vectors;
dimension checks alone cannot establish semantic compatibility. Inter-batch
reranker scores retain the legacy approximation and must be replayed in C4.

The gateways are additive: `/healthz` still initializes no inference provider,
and Streamlit continues using its historical runtime. Fake-wire verification is
not a live provider or RAG quality evaluation. Run it without credentials or DB:

```bash
uv run --package assistant-rh-api --group dev python -m pytest apps/api/tests/gateways -q
```

The `core/errors/` package groups errors by domain: `base`, `storage`,
`inference`, `rag` and `access`. Its `__init__.py` re-exports the same classes,
so existing `from assistant_rh_api.core.errors import ...` imports remain valid.
Domain modules depend on `errors.base`, not on the package re-exports.

## Query classification outcomes (C2, #459)

`QueryProcessor.process()` returns `QueryProcessing(result, diagnostics)`.
Expected inference outages and unusable LLM replies preserve the historical
fallback: `rag_query`, confidence `0.5`, original NFC query, no enrichment or
acronym expansion, `needs_legal_search=false`, LLM flag `null`, and
`should_proceed=true`. Out-of-scope intents remain normal classifications.

The future C6 orchestration must consume these diagnostics explicitly:

| Field | Meaning |
| --- | --- |
| `classification_status` | `disabled` when gating is off, `completed` for a usable classification (including out of scope), `degraded` for an expected failure. A degraded stage must not be recorded as an ordinary successful classification. |
| `classification_error` | Safe cause: `provider_failure` or `invalid_response`, otherwise `null`. The fallback's `intent_reason` carries the same code, never an exception message. |
| `failed_attempts` | Safe B3 provider/model/error/status evidence for inference failure, including invalid provider envelopes. |
| `completion` | The received completion, if any, retained even when intent parsing fails. Its raw text is internal evidence, not a safe error message to expose. |
| `store_errors` | Existing DB fallback diagnostics; classification status describes classification only. |

`ClassificationFailure` chains the original inference/decoder/conversion cause
internally; only its safe reason enters the returned diagnostics. Parsing keeps
legacy fences, defaults, unknown-intent/theme handling, `bool` coercion and
convertible numeric confidence (without adding a 0–1 range check). Non-object
responses, unusable consumed fields, decoder limits and nonfinite confidence
produce the degraded fallback rather than invalid or unserializable results.

Missing or malformed prompts raise `RAGConfigurationError` with their cause.
Invalid caller data, unexpected implementation/port errors and cancellation
propagate. Provider rejections (such as invalid credentials/model/payload) and
partial completion failures also propagate instead of concealing a configuration
or contract failure. Transient outages and invalid provider envelopes remain
eligible for degraded operation. Prompt/acronym DB fallbacks remain unchanged.
No logging I/O or C6 orchestration is implemented in this step.

## Public authentication (B4, #456)

This implements the current D6/A3 contract, which supersedes #456's original
permanent group-token/admin-bootstrap wording. The public API issues nonrenewable
eight-hour sessions after a group password check. Admin groups, hidden groups,
groups without a password and the structural `default` group cannot log in or
appear in the picker. Invalid ministry policies fail closed. Admin tooling and
password administration remain in the existing Streamlit admin; `/admin/*`,
bootstrap of static API keys, and token rotation commands are outside this v1.

| Route | Access | Result |
| --- | --- | --- |
| `GET /v1/auth/groups` | Public | Display metadata, ordered by descending priority then ascending slug; no password hash or policy internals. |
| `POST /v1/auth/session` | Group slug + password | Bearer, expiration, allowed/default ministries and credential revision. |
| `GET /v1/auth/me` | Bearer | Current group policy and remaining lifetime; never echoes the bearer. |
| `DELETE /v1/auth/session` | Bearer | Revokes the current session; 204 with no body. |

`core/auth.py` owns eligibility, lifetime, scope and session use cases. Its ports
inject stores, password verification, token generation, admission quotas and a
clock. The application lifespan composes these with PostgreSQL and crypto
adapters. `create_app(auth_service=...)` allows deterministic HTTP testing.
Future protected handlers must use the shared `Authenticated` dependency from
`handlers/auth.py`; they call `context.authorize_ministry(...)` before ministry
work. Public login/catalogue and `/healthz` remain unauthenticated. Model-alias
resolution belongs to B5; no model/chat/document route is introduced here.

The bearer is `arhs_` followed by 32 random bytes encoded with URL-safe base64.
Only its SHA-256 digest is persisted and queried through the existing session
primary key; there is no scan of group password hashes and no PBKDF2 on bearer
resolution. Randomness uses Python's [secrets](https://docs.python.org/3/library/secrets.html).
Passwords remain compatible with Streamlit's salted PBKDF2-SHA256 format. One
bounded dummy verification is performed for an absent/ineligible group. Crypto
work runs outside the event loop with a four-worker capacity limit. Passwords
are neither normalized nor stripped. Tokens and stored hashes are excluded from
object representations; response projections never expose hashes. Error responses
use the OpenAI envelope without request inputs or backend messages.

The additive migration `20260908180029_api_public_auth.sql` adds monotonic
`credential_revision` values to groups and sessions. A trigger catches existing
Streamlit password updates and changes to visibility, admin role or ministry
policy. Reverting A → B → A cannot revive old sessions. Session creation locks
and verifies the authenticated credential/revision; reset races fail closed.
Cosmetic metadata updates do not invalidate sessions. Existing group creation
and password-update SQL remain compatible and no existing password is changed.

Login admission is shared in `api_auth_limits` across API workers/replicas:

| Environment variable | Default |
| --- | --- |
| `API_AUTH_SOURCE_LIMIT` | 20 attempts per source per window |
| `API_AUTH_SLUG_LIMIT` | 20 attempts per slug per window |
| `API_AUTH_GLOBAL_LIMIT` | 200 attempts across the API per window |
| `API_AUTH_WINDOW_SECONDS` | 60 seconds; configurable from 1 to 300 |

Admission uses a short PostgreSQL transaction before PBKDF2, including successful
login attempts. No lock is held during password work. Rejected reservations do
not increment counters or extend expiry; 429 includes the remaining `Retry-After`.
Expired counters are purged during admission. The table stores hashed identities,
not raw source addresses/slugs, passwords or bearers. DB failure stops login.
All replicas must use the same quota settings; changing them is a coordinated
configuration change, not a per-request override.

The canonical `assistant-rh-api` entrypoint disables Uvicorn proxy-header
rewriting. The API uses the direct peer address, never arbitrary `Forwarded` or
`X-Forwarded-For`. Behind Streamlit/an ingress, this source may be shared: tune its
quota for aggregate traffic and retain the visitor+slug limiter planned in E1.
An alternative ASGI launcher must likewise disable proxy-header rewriting.
Login bodies are limited to 16 KiB before JSON parsing, including chunked bodies;
passwords are at most 1024 characters. Responses use `Cache-Control: no-store`.
The frontend must keep the bearer in server session state, never in a URL/cookie.

### Session retention

Expired or revoked sessions are eligible for deletion immediately. An indexed
purge removes at most 100 inactive rows in each successful session-creation
transaction. A lifespan-owned worker also removes up to 500 rows at startup and
every 60 seconds, including periods with no logins. Large backlogs drain across
multiple batches; active sessions and audit/chat records are preserved. Locked
rows are skipped and retried by later batches, so replicas do not block each
other. Temporary database errors are logged without details and retried at the
next interval. At shutdown, an in-flight purge finishes its bounded transaction
before the worker exits and the pool closes.

### Deployment and rollback

Apply B2 then the B4 versioned migration through the existing migration runner
before starting the B4 API. Compose and its CI smoke test initialize the same
migrations on synthetic data using `bootstrap-local.txt`. Existing local volumes
need `api:local-reset` to run that initializer. Grant the API runtime role access
to the quota table and DELETE on `api_sessions` for retention, alongside its
existing SELECT/INSERT/UPDATE permissions. No cloud migration is implied by local
test success.

Prefer rolling back the API application while retaining this additive schema;
Streamlit continues using its existing group/password columns. If schema removal
is required, stop every B4 API instance, verify the database target and execute
[`rollback_b4_auth.sql`](../../docs/architecture/hexagonal-split/sql/rollback_b4_auth.sql)
in one transaction (`psql -X -v ON_ERROR_STOP=1 -1 -f ...`). It revokes sessions
before dropping revision checks, preserves groups/passwords and removes only B4
columns, trigger and quotas. Reapplying B4 must not restore those sessions.

Validation covers deterministic core/HTTP behavior, real legacy-format password
verification, the assembled FastAPI lifespan against synthetic PostgreSQL,
concurrent quotas, reset/login races, A → B → A and migration reapply/rollback.
It does not migrate Streamlit to HTTP or validate the production ingress (E1/D4).


## Ministry model catalogue (B5)

`GET /v1/models` uses the common B4 bearer resolver and its current group from
`GroupStore`. `ModelService` intersects that policy with the canonical API
ministries (`masa`, `matte`, `mi`, `mso`), rejects invalid policies, and returns
unique `assistant-rh-<ministry>` ids sorted lexicographically. Responses use the
[OpenAI model list envelope](https://developers.openai.com/api/reference/cli/resources/models),
with `object`, `id`, `created`, and `owned_by`, and are marked `Cache-Control: no-store`.
`created=1755734400` is the fixed catalogue epoch from the v1 contract, independent
of request time; `owned_by` is always `assistant-rh`. No provider models are exposed.

`ModelService.resolve(model, group)` prepares C1 routing: `assistant-rh` resolves
to the configured default and returns a canonical model id and ministry. This
alias is accepted as input only and is not listed. Unknown ids raise 404
`model_not_found`; known ministries outside the group raise 403 `ministry_forbidden`.
Chat Completions is not implemented by B5.

A policy with zero ministries, unknown ministries, or an absent/forbidden default
raises `ministry_configuration_error` in the service, without implicit fallback.
B4 still hides invalid groups and rejects their login. Revoked, expired, or stale
sessions remain 401; an otherwise authenticated current session with corrupt
policy data produces a safe OpenAI-style 500 configuration error. Normal database
policy changes revoke sessions through B4's credential revision before this point.

The API dev dependencies include the OpenAI Python SDK. Tests exercise real
`AsyncOpenAI.models.list()` over the ASGI app, including strict response parsing,
isolation, stable ordering, invalid policies and SDK errors. The PostgreSQL HTTP
lifespan test also lists models after password login and rejects a revoked bearer.


### Configuration RAG par requête (préparation C2–C6)

Le lifespan assemble `app.state.rag_configuration_service` avec le store DB B2 ;
une instance peut être injectée dans `create_app` pour les tests. Le démarrage charge et valide un
premier snapshot de `rag_config` sans le conserver comme cache de requête. Une
configuration de type invalide bloque le démarrage et ferme le pool. À l'entrée du futur moteur, appeler
`await service.load()` une seule fois et garder `result.config` jusqu'à la fin de
la requête. Le snapshot contient la configuration RAG profondément immuable, la
révision du store et son origine. Le prochain appel relit la DB et voit les
modifications admin ; les requêtes en cours conservent leurs valeurs.

`result.fallback` distingue ligne absente, indisponibilité et échec DB lors du
repli historique vers les défauts. Les types incorrects lèvent
`RAGConfigurationError` sans divulguer les valeurs. Cette tranche reproduit le
mapping historique des paramètres, y compris ses défauts et fallbacks d'enums ;
elle ne revalide pas les plages de l'interface admin. Les prompts, acronymes,
lectures runtime supplémentaires et overrides environnement des tables restent
à composer dans C2/C3/C5 ; C6 doit brancher le snapshot au moteur. Aucune route
completion ni modification du runtime Streamlit n'est livrée ici.
