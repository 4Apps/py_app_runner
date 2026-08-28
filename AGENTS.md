# py_app_runner

Async Python service framework: Tornado HTTP/WS bridge, dynamic service loader (PyBridge),
Redis-backed WebSocket connection manager. Used as a library by 4Apps backend projects.

## Build & Run

```bash
docker compose up develop          # Postgres (main_db) + Redis (cache_db) + dev container
docker compose exec develop bash

pip install py_app_runner          # as a dependency elsewhere
pip install --pre py_app_runner    # include develop-branch pre-releases

/srv/meta/scripts/code_tests.bash  # tests, inside the container
ruff check src/ && ruff format src/
pyrefly check ./src/
```

Postgres and Redis are test dependencies, not runtime ones. An unreachable server is a test
**failure**, not a skip; `ALLOW_PG_SKIP` / `ALLOW_REDIS_SKIP` opt into skipping on a bare checkout.

## Versioning & Release

`major.minor` is manual in `.version` (read by `pyproject.toml` via
`[tool.setuptools.dynamic]`); the patch part is the git commit count, appended by CI.

- `scripts/version.bash` — prints `<major.minor>.<git rev-list --count HEAD>`; `--dev` appends
  `.dev0`. Publish workflows write it into `.version` before building, workspace-only. Needs
  `fetch-depth: 0`; refuses a shallow clone.
- `scripts/bump_version.bash [major|minor]` — bumps and stages `.version`. Never bump the patch by hand.
- `scripts/release_tag.bash` — creates `v<version>`, publishes nothing; refuses a tag sorting
  below the newest existing one (remedy: bump the minor).
- `py_app_runner.__version__` comes from installed package metadata.

**A PyPI version can never be replaced or re-uploaded**, and a rebase/squash/reset shortens
history into an already-published version. For that reason, **merge develop into master with
`--no-ff`**. Skipped commits leave harmless gaps in the sequence.

| Trigger | Version | pip resolves it |
| --- | --- | --- |
| push to `develop` | `0.4.50.dev0` | only with `--pre`, or an exact pin |
| GitHub release on `v0.4.52` | `0.4.52` | as "latest" |

The old `4apps.lv` wheel server is retired - move pins to `py_app_runner==<version>`.

### CI

- `test.yml` — pushes to `master`, all PRs, `workflow_call`. Both publish workflows call it;
  `workflow_call` reuses the definition, not the result, so a release re-runs the suite
  against the tagged tree. Not `develop` - `publish-dev.yml` already calls it there.
- `publish-dev.yml` — every push to `develop` except markdown and `specs/`. `skip-existing` on,
  concurrency queues rather than cancels.
- `publish.yml` — **`release: published` only**, no `skip-existing`. Refuses a tag that
  disagrees with the derived version, and refuses a GitHub pre-release.

```bash
scripts/release_tag.bash          # creates v0.4.52 locally
git push origin v0.4.52           # publishes nothing
gh release create v0.4.52 --title v0.4.52 --generate-notes   # point of no return
```

All three run on `ubuntu-latest`. The repository is public, and GitHub's org runner groups
refuse public repositories by default - a bare `pull_request` trigger on a self-hosted
runner lets a fork's PR execute code on an internal machine. Hosted runners are free for
public repositories and each job gets a disposable VM, at the cost of no layer cache.
`test.yml` derives `LOCAL_USER_ID`/`LOCAL_GROUP_ID` from the runner user. `.env` supplies
them locally and is gitignored, so CI would otherwise fall back to the Dockerfile's default
of 1000 while the runner user is 1001, leaving `appuser` unable to write into the
bind-mounted workspace.

Both publish jobs use trusted publishing (OIDC, `id-token: write`); no PyPI token is stored.
Publisher identity is owner + repo + workflow *filename* + environment - `publish.yml` in
`release`, `publish-dev.yml` in `release-dev`. **Renaming either file breaks publishing.**

## Architecture

```
Client (HTTP or WS)
  → bridge (api.py or websocket.py)
  → find_service(service_name) via PyBridge cache
  → service._service_pybridge.bridge_request(action, request_data, bridge_handler)
  → RequestHandlerHelper.handle_request() dispatches to @action method
  → response dict → write_custom_message() wraps in {"data": ...} → encoded → sent
```

- **`runner.py`** — CLI entrypoint (`main()`): args, logging/Sentry, service loading, event loop (uvloop in prod).
- **`pybridge.py`** — Dynamic loader. `services.<name>._service` / `_service_args` /
  `_service_pybridge`, falling back to `py_app_runner.<name>.<module>` for built-ins (`bridge`,
  `migrations`). Project services win, so a project can override a built-in by name.
- **`registry.py`** — `AppRegistry` singleton (config, models, Redis channel, web app class). Configure before start.
- **`config.py`** — `load_config()` maps `ENV_VAR` names into nested dict keys by splitting on
  `_`; only vars whose first key exists in defaults are processed. `is_env_dev/test/prod`.
- **`db_pools.py`** — PostgreSQL + Redis pool management.
- **`bridge/`** — `web_app.py` (Tornado app), `api.py` (`ApiHandler`), `websocket.py`
  (`BaseWebSocketHandler`: lifecycle, token cache, heartbeat), `encoders/` (msgspec JSON /
  MessagePack, chosen by a class attribute), `_service.py` (startup; forks workers in prod,
  single-process autoreload in dev).
- **`request_handler/`** — `handlers.py` has two distinct bases: `RequestHandlerBase` /
  `WebHandlerBase` are the real Tornado handlers carrying request context (the `bridge_handler`);
  `RequestHandlerHelper` is a plain class for action dispatch and is what services extend. Also
  `decorators.py`, `auth_service.py` (JWT: user, device, impersonation), `pagination.py`
  (`parse_pagination()`).
- **`wbcm/`** — Redis pub/sub relay across instances. `wb_connection_manager.py` (background
  thread, routes via `call_soon_threadsafe`), `factory.py` (`UserConnections`),
  `device_connections.py` (`DeviceConnections`), `ws_interface.py` (`WebSocketHandlerInterface` ABC).
- **`http_exception.py`** (`HTTPException.to_dict()`), **`return_model.py`**
  (`StatusModel`/`MessageModel`/`ReturnModel`),
  **`tick_service.py`** (periodic base with graceful shutdown), **`timer.py`**, **`utils.py`**
  (`json_encode/decode`, `sha256_hash`, `generate_random_string`).

## Migrations (`migrations/`)

Applies tracked SQL files to one or more databases. Add `migrations` to `SERVICES` to enable.

```
python3 src/app.py migrations status   [--check] [--target NAME]
python3 src/app.py migrations apply    [--dry-run] [--to PREFIX] [--target NAME]
python3 src/app.py migrations baseline [--to PREFIX] [--yes] [--target NAME]
python3 src/app.py migrations new      <name> [--target NAME]
python3 src/app.py migrations repair   <filename> [--target NAME]
```

Default is one target `main` on `config["db"]["main"]`, files `YYYY-MM-DD-HHMMSS-kebab-name.sql`
in `config["migrations"]["dir"]` (`data/migrations`, relative to `current_path`), tracked in
`config["migrations"]["table"]` (`migrations`). An absent key, an empty one, and the flat
`{"dir", "table"}` shape all mean that single target - **no project needs a config change.**

A second database opts in with `targets`; `db` defaults to the target name, `dir`/`table` as above.
Mixing `targets` with a top-level `dir`/`table` is rejected.

```python
"migrations": {"targets": {
    "main": {"db": "main", "dir": "data/migrations", "table": "migrations"},
    "gis":  {"db": "gis", "dir": "data/migrations_gis", "table": "gis_migrations"},
}}
```

- No `--target`: `status`/`apply` run every target in order; `apply` stops at the first failure,
  `status --check` reports all and exits 1 if any is pending/blocked. Output is `[name] `-prefixed
  only when processing more than one.
- `new`/`repair`/`baseline` require `--target` once more than one exists. Unknown target exits 1.
- Targets run strictly in sequence, each taking the same advisory lock on its own connection.
- Two targets on one physical database **must not share a tracking table** - refused by
  `resolve_targets`, since each would see the other's rows as `MISSING`.
- Each file runs in its own transaction, tracking row written inside it. Migrations need not be idempotent.
- `-- migrations:no-transaction` on line 1 (for `CREATE INDEX CONCURRENTLY`) requires **exactly
  one statement** - Postgres wraps multi-statement sends in an implicit transaction. `apply`
  refuses at pre-flight (crude count: strip `--`, split on `;`). A failed one is not recorded and
  may have partially applied.
- `DRIFT` (sha256 mismatch) and `MISSING` (tracked file deleted) block `apply`. `repair` fixes
  DRIFT only; MISSING needs the file restored, or `DELETE FROM migrations WHERE name = '<name>';`
  which `apply` prints.
- Refused up front: psql meta-commands (`pg_dump`'s `\restrict`), and an unrelated pre-existing
  `migrations` table (created `IF NOT EXISTS`, then column-checked).
- `baseline` writes tracking rows without executing anything.
- Every subcommand exits non-zero on failure. `runner.py` would exit 0, so `_service.py` converts
  non-`SystemExit` failures to `SystemExit(1)` and catches `KeyboardInterrupt` /
  `CancelledError` separately ahead of it. **Do not simplify either clause away.**
- `commands.py` is importable; `cmd_apply`/`cmd_baseline`/`cmd_repair` require autocommit.

## Field Encryption (`crypto/`)

Explicit encryption of columns the app reads back, plus the E2EE binary envelope. Add `crypto`
to `SERVICES` for the CLI; the library imports without it.

```bash
python3 src/app.py crypto key
python3 src/app.py crypto rotate --table T --column C [--id id] [--batch N] [--dry-run]
```

`config["crypto"]` = `{"key": "k1", "keys": {"k1": "APP_CRYPTO_K1"}, "index_key": "APP_CRYPTO_INDEX"}`

- `FieldCrypto.encrypt()` / `decrypt()` / `blind_index()`. Nothing is hooked into the database layer.
- Format `pa1:<key_id>:<base64 of nonce(12) || ct || tag(16)>`, AES-256-GCM. **Version and key id
  are bound as AAD.** The estate's other format, `sp1:`, is a different cipher and is refused, never misparsed.
- Config holds only env var *names*. `decrypt` reads the key id off the value, so a retired key
  keeps working while listed in `keys` - that is the rotation mechanism, and removing an id is permanent.
- `decrypt(None)`→`None`, `decrypt("")`→`""`, non-`pa1:` returned verbatim (so a column can hold
  both mid-backfill). A `pa1:` value that will not open always raises.
- `blind_index()` gives back equality lookups via a second, separately keyed column - not ranges
  or `LIKE`, and it deliberately does not normalise case or whitespace.
- `envelope.py` — Python side of `magic(3) | version(1) | nonce(12) | ct | tag(16)`, the format
  `copasty-server` clients write. `validate()` is the check a keyless server can still make.
- `PasswordHasher` wraps bcrypt with `needs_rehash()`. No salt column - bcrypt embeds it.

## Audit Trail (`audit/`)

One row per recorded change, written explicitly. No ORM hook, no middleware, no trigger.

```bash
python3 src/app.py audit install [--dir PATH] [--table NAME]
python3 src/app.py audit prune --before YYYY-MM-DD [--batch N] [--dry-run]
```

`config["audit"]` = `{"db": "main", "table": "audit_log", "strict": True, "max_rows": 1000,
"id_key": "id", "exclude": {"users": ["password"]}}`

- **The audit row is written on the caller's cursor, inside the caller's transaction** - a
  rolled-back change takes its row with it. No buffering, no batching, no shutdown flush.
  **A refactor must not defer the write.**
- `update()`/`delete()` read affected rows before writing, so old values need no hand-written
  before-fetch. `max_rows` is checked on that read, before anything is written.
- `insert()` always uses `RETURNING`; any other way of getting the id means guessing at a sequence,
  and the failure mode is an empty `entity_id`.
- Comparison is neither `==` nor `str()` - `diff.same()` normalises per type, since psycopg
  returns real types. The boolean truthy table applies **only when one side is a real bool**,
  which stops a text `"true"` equalling `"1"`.
- Request context is a per-request `ContextVar` (`request_context(...)`), never process-wide.
- `strict` (default on) raises on a failed write. With it off, note a failed INSERT still aborts
  the surrounding Postgres transaction - use a savepoint.
- A malformed `exclude` block is refused at construction; redaction fails open by nature.
- Columns are cut to fit rather than rejected.
- `audit install` writes the schema into the *project's* migrations directory - the framework
  ships no migration of its own.

## Job Queue (`queue/`)

```bash
python3 src/app.py queue install [--dir PATH]
python3 src/app.py queue work [--queue a,b] [--once] [--stop-when-empty]
                              [--max-jobs N] [--max-time N] [--timeout N] [--sleep N]
python3 src/app.py queue status | failed [--limit N] | retry (--id N|--all) | forget (...)
```

`config["queue"]` = `{"driver": "database", "db": "main", "table": "queue_jobs",
"failed_table": "queue_failed_jobs", "queue": "default", "tries": 3, "backoff": [10, 60, 300],
"timeout": 300, "sleep": 1.0, "handlers": {}}`

- **A push is an `INSERT`, so it joins the transaction that caused it** - the whole argument for
  keeping jobs in the application's own database.
- Reserving is a candidate `SELECT ... FOR UPDATE SKIP LOCKED` then a guarded `UPDATE` in one
  transaction, retried five times. **The guard in the `WHERE` - not `SKIP LOCKED` - is what makes
  the claim safe.** Do not simplify it away.
- A claim is a **deadline, not a flag**: `reserved_until` in the past means the worker died, so
  the job is claimable with its attempt already spent. No heartbeat, no lease renewal.
- The handler runs with **no queue transaction open**.
- **Reserving is the attempt** - `attempts` increments on claim, first `handle()` sees 1, and
  `release()` never touches it.
- Queue precedence is sequential, not a merged sort: `["high", "default"]` drains `high` first.
- Every time comparison binds an application-computed UTC value, never `now()`.
- Uniqueness is scoped to pending, released on completion and failure; one partial unique index.
- Payloads are JSON, never pickle - an unpickle here would be RCE in a table. An undecodable
  payload goes straight to failed.
- Per-job timeout is `asyncio.timeout()`, which cancels a task blocked in a query; the visibility
  timeout backstops a worker that dies outright.
- Handler names resolve as `module:attr` or a configured alias, alias first - a renamed class
  keeps working without touching the rows naming it.
- Exit contract: `0` for every ordinary end, `1` only for repeated reserve failures. A failed
  *job* never changes the exit code.

### The Redis driver

`config["queue"]["driver"] = "redis"` and nothing else moves. Both drivers satisfy `QueueDriver`
in `interface.py`; `tests/test_queue_contract.py` runs one suite against both. Add
`{"hostname":..., "port": 6379, "database": 0, "password":..., "prefix": "queue:", "group":
"workers"}` - deliberately **not** the cache connection, which is one `FLUSHDB` from an empty backlog.

- **Streams, not lists**: a consumer group keeps every delivered entry until acknowledged, and
  `XAUTOCLAIM` returns one after the visibility timeout.
- One stream per priority (`q:{queue}:s:{n}`), a sorted set for scheduled, a hash per job. Stream
  entries are immutable, so the stream indexes what is *ready* and carries only the id; the hash is the job.
- `XAUTOCLAIM` runs **before** `XREADGROUP` per level, so a dead worker's job beats new work.
- The group is created at `0`, not `$` - jobs are pushed before any worker starts.
- `XAUTOCLAIM` knows idle time, not liveness. `reserved_until` is authoritative, so an entry
  reclaimed while its claim is valid is **parked** back into the delayed set, costing no attempt.
- Min-idle-time comes from the reclaiming worker, so `timeout` must be one value fleet-wide.
- One shared consumer name for the whole fleet; who holds a job is in `reserved_by`.
- A failed job **keeps its id** (unlike the database driver), so `queue retry` restores the job
  that failed. `retry --id` on a job that is not failed does nothing.
- `queue install` prints "nothing to install" and exits 0, so a playbook can run it either way.
- Needs Redis 6.2+ (`XAUTOCLAIM`), **not cluster aware**.
- **Cannot** join the transaction that caused the push - it is a second system. Use it when
  volume warrants it or losing a job is survivable.

## Rate Limiting (`throttle/`)

Fixed-window counting on Redis. `@rate_limit` is built on it and keeps its signature.

```python
attempt = await Throttle(redis_con).hit(f"login:{email}", 5, 900)
if not attempt.allowed:
    raise HTTPException("Too many attempts", code=4029, http_status=429)

await throttle.clear(f"login:{email}")   # the moment the protected thing succeeds
```

`config["throttle"]` = `{"prefix": "throttle:", "fail_open": True}`

- The window opens on the first hit and closes `window` seconds later; the known cost is the
  boundary - 5 per 15 min has a worst case of 10 in quick succession.
- `hit()` counts allowed or not, so hammering neither resets nor extends the window. `check()`
  peeks without counting. `clear()` on success is what stops a near-lockout after a typo.
- `Attempt` carries `allowed/limit/hits/remaining/retry_after/reset_at` and `headers()`
  (`X-RateLimit-*`, plus `Retry-After` only when denied).
- Counting is a single Lua script, so it is **exact** at the same one round trip.
- Stored state is `{hits, reset}`; the **reset timestamp is authoritative**, not the key's TTL.
- Keys are stored as `sha256(caller_key)` - it is routinely an email or IP.
- `fail_open` defaults to true; the line is always logged.

## Decorator Stack

**Order matters — outermost first, applied bottom-up:**

```python
@action("verb")           # Register as routable action (MUST be outermost)
@authenticated            # Require bridge_handler.current_user is not None
@with_db                  # Inject self.pg_conn, self.pg_cur, self.db_wrapper
@with_tx                  # PostgreSQL transaction (MUST come after @with_db)
async def method(self, input_data: dict) -> dict:
```

- `@with_cache` — inject `self.redis_con`; `@with_cache_and_db` — both
- `@rate_limit(max_requests, window_seconds)` — MUST come after `@with_cache`
- `@require_auth_for_actions` — class decorator applying `@authenticated` to all `@action`
  methods, inherited ones included (dispatch resolves across the MRO, so guarding only the
  class's own methods would leave base-class actions open)

## Error Handling & Protocol

`raise HTTPException("message", code=-404, http_status=404)`

The `data` field is the sole payload container; `msg_id`, `service`, `auth_token` and
`device_session_token` are protocol envelope only. Errors are wrapped in `data` like everything else.

```json
// Client → Server
{"msg_id": 1, "service": "name", "auth_token": "jwt...",
 "data": {"action": "action_name", "data": {"...payload..."}}}

// Server → Client, success then error
{"msg_id": 1, "service": "name", "data": {"status": "ok"}}
{"msg_id": 1, "service": "name", "data": {"error": {"code": -404, "msg": "Not found"}}}
```

This holds for HTTP too - `WebHandlerBase.error()` wraps both `HTTPException` and plain string
errors in `data`. Status codes from `ApiHandler`: `HTTPException` → its own `http_status`; any
other exception → `500`, never a 4xx; an action returning `None` → `204` with no body, mirroring
the WebSocket path.

Over HTTP the JSON body *is* the request; the query string may only name the `action` (the URL
path wins). Nothing else from the URL reaches a service - it would sit in access logs and caches
as input. The API key is read from `X-API-Key` only, never `?api_key=`; a browser WebSocket
client, which cannot set headers, sends `api_key` in its first message instead.

`log_request()` writes headers and the body at WARNING on every `HTTPException`, so both go
through key-based redaction: any header name or JSON key containing `authorization`, `cookie`,
`password`, `passwd`, `secret`, `token`, `api-key`/`api_key`/`apikey` or `credential` is logged
as `<redacted>` (`utils.is_sensitive_key`). Substring on purpose - a project's `X-Auth-Token` or
`refresh_token` is covered without the framework knowing the name. Over-redaction in a log is
the cheap failure; extend the marker list rather than special-casing a call site.

## Downstream Integration

1. **`app.py`** — configure `AppRegistry` (config, models, Redis channel), call `main()`.
2. **`config.py`** — `load_config(defaults=..., split_value_keys=[...])`.
3. **`src/services/<name>/_service_pybridge.py`** — export `bridge_request(action, request_data, bridge_handler)`.
4. **Handlers** — extend `RequestHandlerHelper`, use `@action` + the decorator stack.
5. Optional: custom `WebSocketHandler` (extends `BaseWebSocketHandler`), custom `WebApplication`.

**`RequestHandlerHelper` instances are per-request.** `handle_request()` stores `bridge_handler`
on `self`, and `@with_db`/`@with_cache` attach then delete `pg_conn`/`pg_cur`/`db_wrapper`/
`redis_con` on `self`. Construct a fresh handler inside `bridge_request()` - a module-level
singleton would let concurrent requests overwrite each other's connections and `current_user`.

Config keys the framework reads:

- `api.key` (str or list) — static API key(s) when `api_key_use_db=False`
- `api_key_pepper` — pepper for hashed API keys when `api_key_use_db=True`
- `jwt.secret` — HS256 signing secret
- `ws_allowed_origins` (str or list) — accepted WebSocket `Origin`s; falls back to the
  `WS_ALLOWED_ORIGINS` env var. Empty accepts any origin and logs a warning in prod.
- `sentry.dsn`, `sentry.rate.performance`, `sentry.rate.profiles`

The env-var mapping splits on `_`, so a nested path must not run through a key already holding a
scalar (`API_KEY_PEPPER` cannot coexist with `api.key`). Such a variable is logged and skipped.

## Key Paths & Style

```
src/py_app_runner/          # Package source
docker/app/Dockerfile       # Multi-stage (base -> builder -> development)
scripts/                    # version, release_tag, bump_version, pre-commit
docker-compose.yml          # Development service
pyproject.toml              # Metadata, dependencies, ruff config
.version                    # Manual major.minor; CI appends the commit count
LICENSE                     # MIT, shipped in the distributions
```

- Python 3.11+, async/await with Tornado and uvloop
- Ruff: line-length 120, rules E/F/I/B/UP. Double quotes, space indentation
- Type hints on all function signatures
- Always use braces/blocks for conditionals (no single-line ifs)
- File-scoped imports only — never inside functions
