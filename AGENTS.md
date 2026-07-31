# py_app_runner

Async Python service framework providing Tornado HTTP/WS bridge, dynamic service loader (PyBridge), and Redis-backed WebSocket connection manager. Used as a library dependency by 4Apps backend projects.

## Build & Run

```bash
# Development (Docker)
docker compose up develop
docker compose exec develop bash

# Install as dependency in another project
pip install git+ssh://git@github.com/4Apps/py_app_runner.git

# Run tests inside container
/srv/meta/scripts/code_tests.bash

# Lint
ruff check src/
ruff format src/

# Type check
pyrefly check ./src/
```

## Versioning

`major.minor` is manual and lives in `.version`; the patch part is the git commit
count, appended by CI at build time.

- `.version` — the only place the version is declared. `pyproject.toml` reads it via
  `[tool.setuptools.dynamic]`, so a local build reports plain `major.minor`.
- `.github/workflows/publish.yml` rewrites `.version` to `<major.minor>.<git rev-list --count HEAD>`
  before building, which stamps the wheel filename and package metadata. The rewrite is
  workspace-only, never committed. Checkout needs `fetch-depth: 0` or the count is wrong.
- `scripts/bump_version.bash [major|minor]` — bumps and stages `.version`. Nothing bumps
  the patch part by hand.
- `py_app_runner.__version__` resolves from installed package metadata, so it always
  matches the wheel that was actually installed.

Publishing: the runner is on processing, so the workflow scp's the wheel to
`services@4apps.lv:/srv/sites/4apps.lv/www/Application/Public/apps/py/` and repoints the
`py_app_runner-latest-py3-none-any.whl` symlink at it. Downstream projects pin an exact
versioned wheel URL, so publishing never changes what an existing project resolves.

## Architecture

### Request Flow

```
Client (HTTP or WS)
  → bridge (api.py or websocket.py)
  → find_service(service_name) via PyBridge cache
  → service._service_pybridge.bridge_request(action, request_data, bridge_handler)
  → RequestHandlerHelper.handle_request() dispatches to @action method
  → response dict → write_custom_message() wraps in {"data": ...} → encoded → sent
```

### Core Components

- **`runner.py`** — CLI entrypoint (`main()`). Parses args, sets up logging/Sentry, loads services via PyBridge, starts the async event loop (uvloop in prod).
- **`pybridge.py`** — Dynamic service loader. Imports service modules from
  `services.<name>._service` / `_service_args` / `_service_pybridge`, falling back to
  `py_app_runner.<name>.<module>` for built-ins (`bridge`, `migrations`). Project services
  win, so a project can override a built-in by shipping its own module of the same name.
- **`registry.py`** — `AppRegistry` singleton. Holds project-specific config, model classes, Redis channel, and web app class. Must be configured before runner starts.
- **`config.py`** — `load_config()` loads `.env`, maps `ENV_VAR` names into nested dict keys by splitting on `_`. Only vars whose first key exists in defaults are processed. Helpers: `is_env_dev/test/prod`.
- **`db_pools.py`** — Database connection pool management (PostgreSQL + Redis).

### Migrations (`migrations/`)

Built-in service that applies tracked SQL files to `config["db"]["main"]`. Enabled by
adding `migrations` to `SERVICES`; omit it and nothing is imported.

```
python3 src/app.py migrations status   [--check]
python3 src/app.py migrations apply    [--dry-run] [--to PREFIX]
python3 src/app.py migrations baseline [--to PREFIX] [--yes]
python3 src/app.py migrations new      <name>
python3 src/app.py migrations repair   <filename>
```

Files live in `config["migrations"]["dir"]` (default `data/migrations`, relative to
`current_path`) and are named `YYYY-MM-DD-HHMMSS-kebab-name.sql`. The timestamp prefix both
orders them and keeps two feature branches from colliding the way sequential numbers do.
The tracking table is `config["migrations"]["table"]` (default `migrations`) - override it
when that name is already taken by something else in the database.

- Each file runs in its own transaction, with its tracking row written inside that same
  transaction - a migration either fully lands and is recorded, or neither. Put
  `-- migrations:no-transaction` on line 1 for `CREATE INDEX CONCURRENTLY` and friends.
- **A no-transaction file must contain exactly one statement.** Postgres wraps a
  multi-statement simple-Query send in an *implicit* transaction, so the directive would
  silently not take effect; the whole file goes to `cur.execute()` in one call and there is
  no statement splitter. `apply` counts statements during its pre-flight scan and refuses
  such a file before executing anything. The count is crude (strip `--` comments, split on
  `;`), so it over-counts a dollar-quoted body - acceptable, since that is a loud refusal at
  scan time and no dollar-quoted function body needs this directive.
- A **failed** no-transaction file cannot roll back and is not recorded. `apply` says so
  explicitly: it may have partially applied (a failed `CREATE INDEX CONCURRENTLY` leaves an
  invalid index behind, and a re-run then dies on `relation already exists`), so inspect the
  database before re-running.
- Because what ran is recorded, **migrations do not need to be idempotent**.
- A sha256 of each file is stored; editing an applied file shows as `DRIFT` and blocks
  `apply` until it is reverted or `repair`ed. A tracked migration whose file has since been
  deleted (a rebase, a squash, someone pruning old files) shows as `MISSING` and blocks
  `apply` the same way - the database claims to have run something the repository can no
  longer show, so nobody can tell whether the schema still matches.
- `repair` is the DRIFT remedy only; it re-reads the file to re-stamp its checksum, so for
  `MISSING` it can only answer "no such migration file". The remedies for `MISSING` are to
  restore the file from version control, or - if it is gone for good and its schema change
  is known to be in place - to delete the tracking row by hand:
  `DELETE FROM migrations WHERE name = '<name>';`. `apply` prints that statement, with the
  configured table name filled in, when it blocks on a `MISSING` state.
- The tracking table is created with `CREATE TABLE IF NOT EXISTS`, which matches on name
  alone. Its columns are verified straight afterwards, so an unrelated pre-existing
  `migrations` table is reported as such - naming the missing columns and pointing at the
  `config["migrations"]["table"]` override - rather than being adopted and failing later
  with a bare `column "name" does not exist`.
- Files must not contain psql meta-commands. `pg_dump` emits `\restrict` / `\unrestrict`,
  and psycopg has no psql to interpret them - `apply` refuses such a file up front.
- `apply` holds a session advisory lock for the whole run, so two containers starting at
  once serialise instead of racing.
- `baseline` is how an existing database adopts the system: it writes tracking rows without
  executing anything.
- `status --check` exits 1 if anything is pending or drifted/missing, so a deploy script
  can assert a clean state without parsing output. Every subcommand exits non-zero on
  failure, which is what makes `apply` safe to run unattended in a playbook that must halt
  before restarting services against a half-migrated database. `runner.py` catches
  `Exception` around `init_service` and returns normally, which would exit 0 - deliberate
  for a long-running service, fatal here - so `migrations/_service.py` converts any
  non-`SystemExit` failure (unreachable database, permissions error, dropped connection)
  into `SystemExit(1)` itself. Do not "simplify" that away.
- `commands.py` is importable directly for programmatic use (for example, building a
  throwaway database in a test harness). `cmd_apply`, `cmd_baseline` and `cmd_repair`
  require an autocommit connection and refuse otherwise, since each opens and closes its
  own per-file transactions rather than running inside one the caller opened.

### Bridge Subsystem (`bridge/`)

Tornado-based HTTP and WebSocket server:
- **`web_app.py`** — Tornado web application setup.
- **`api.py`** — `ApiHandler` for HTTP requests. Auto-wraps responses in `{"data": ...}`.
- **`websocket.py`** — `BaseWebSocketHandler` for WebSocket connections. Manages connection lifecycle, auth token caching, heartbeat, message encoding.
- **`encoders/`** — Pluggable encoders (JSON via msgspec, MessagePack). Encoder is a class attribute on the handler.
- **`_service.py`** — Bridge startup: loads services, creates web app, forks workers (prod) or runs single-process with autoreload (dev).

### Request Handlers (`request_handler/`)

- **`handlers.py`** — Two distinct base classes:
  - `RequestHandlerBase` / `WebHandlerBase` — actual Tornado RequestHandlers. Carry request context (`current_user`, `db_pools`, etc.). These are the `bridge_handler`.
  - `RequestHandlerHelper` — plain Python class for action dispatch. NOT a Tornado handler. Gets `bridge_handler` passed in. This is what service handlers extend.
- **`decorators.py`** — Route/auth decorators (see Decorator Stack below).
- **`auth_service.py`** — JWT creation/verification (user, device, impersonation tokens).
- **`pagination.py`** — `parse_pagination()` helper.

### WebSocket Connection Manager (`wbcm/`)

Redis-backed pub/sub for multi-instance WebSocket message relay:
- **`wb_connection_manager.py`** — Runs in background thread. Reads Redis stream, routes messages to local connections via `call_soon_threadsafe`.
- **`factory.py`** — `UserConnections`: uid→connection mapping, user_id→[uid] index.
- **`device_connections.py`** — `DeviceConnections`: device_id→connection tracking.
- **`ws_interface.py`** — `WebSocketHandlerInterface` ABC.

### Supporting Modules

- **`http_exception.py`** — `HTTPException(message, code, http_status)` with `to_dict()`.
- **`return_model.py`** — `StatusModel`, `MessageModel`, `ReturnModel` for standardized responses.
- **`tick_service.py`** — Periodic tick service base with signal handling and graceful shutdown.
- **`timer.py`** — Hierarchical performance timer with table/CSV output.
- **`utils.py`** — `json_encode/decode` (msgspec), `sha256_hash`, `generate_random_string`, type conversion helpers.

## Decorator Stack

**Order matters — outermost first, applied bottom-up:**

```python
@action("verb")           # Register as routable action (MUST be outermost)
@authenticated            # Require bridge_handler.current_user is not None
@with_db                  # Inject self.pg_conn, self.pg_cur, self.db_wrapper
@with_tx                  # Wrap in PostgreSQL transaction (MUST come after @with_db)
async def method(self, input_data: dict) -> dict:
```

Other decorators:
- `@with_cache` — inject `self.redis_con`
- `@with_cache_and_db` — both Redis + PostgreSQL
- `@rate_limit(max_requests, window_seconds)` — Redis-backed rate limiter (MUST come after @with_cache)
- `@require_auth_for_actions` — class decorator, applies `@authenticated` to all @action
  methods, inherited ones included (dispatch resolves actions across the MRO, so
  guarding only the class's own methods would leave base-class actions open)

## Error Handling

```python
raise HTTPException("message", code=-404, http_status=404)
```

Error responses are wrapped inside `data`, like all responses:
```json
{"msg_id": 1, "service": "...", "data": {"error": {"msg": "...", "code": -404}}}
```

Success responses:
```json
{"msg_id": 1, "service": "...", "data": {"status": "ok"}}
```

The `data` field is the sole payload container — `msg_id` and `service` are protocol envelope only.

This holds for HTTP too: `WebHandlerBase.error()` wraps both `HTTPException` and plain
string errors in `data`, so an HTTP client parses `data.error` exactly like a WebSocket
client. The HTTP status still carries `http_status` from the exception.

HTTP status codes from `ApiHandler`:

- `HTTPException` → its own `http_status`
- any other exception → `500` (a server-side fault, never a 4xx)
- action returns `None` → `204` with no body, mirroring the WebSocket path, which
  simply sends nothing in the same case

## WebSocket Protocol

Messages have two layers:
- **Protocol envelope**: `msg_id`, `service`, `auth_token`, `device_session_token` — routing, correlation, authentication
- **Payload**: `data` — ALL application content (success or error)

### Client → Server
```json
{
  "msg_id": 1,
  "service": "service_name",
  "auth_token": "jwt...",
  "data": {"action": "action_name", "data": {"...payload..."}}
}
```

### Server → Client (Success)
```json
{"msg_id": 1, "service": "service_name", "data": {"...response..."}}
```

### Server → Client (Error)
```json
{"msg_id": 1, "service": "service_name", "data": {"error": {"code": -404, "msg": "Not found"}}}
```

## Downstream Integration

Projects integrate by:

1. **`app.py`** — Configure `AppRegistry` with project config, models, Redis channel. Call `main()`.
2. **`config.py`** — `load_config(defaults=..., split_value_keys=[...])` to load `.env` into nested dict.
3. **`src/services/<name>/_service_pybridge.py`** — Export `bridge_request(action, request_data, bridge_handler)`.
4. **Handler classes** — Extend `RequestHandlerHelper`, use `@action` + decorator stack.
5. **Optional overrides**: custom `WebSocketHandler` (extends `BaseWebSocketHandler`), custom `WebApplication` (extends `WebApplication`).

**`RequestHandlerHelper` instances are per-request.** `handle_request()` stores
`bridge_handler` on `self`, and `@with_db` / `@with_cache` attach and then delete
`pg_conn` / `pg_cur` / `db_wrapper` / `redis_con` on `self`. Construct a fresh handler
inside `bridge_request()`; a module-level singleton would let concurrent requests
overwrite each other's connections, transactions and `current_user`.

### Config keys the framework reads

- `api.key` (str or list) — static API key(s) when `api_key_use_db=False`
- `api_key_pepper` — pepper for hashed API keys when `api_key_use_db=True`
- `jwt.secret` — HS256 signing secret
- `ws_allowed_origins` (str or list) — accepted WebSocket `Origin` values; falls back to
  the `WS_ALLOWED_ORIGINS` env var. Empty means accept any origin, which logs a warning
  in prod.
- `sentry.dsn`, `sentry.rate.performance`, `sentry.rate.profiles`

Note the env-var mapping splits on `_`, so a nested key path must not run through a key
that already holds a scalar (`API_KEY_PEPPER` cannot coexist with `api.key`). Such a
variable is logged and skipped rather than silently clobbering the scalar.

## Key Paths

```
src/py_app_runner/          # Package source
docker/app/Dockerfile       # Multi-stage Dockerfile (base -> builder -> development)
scripts/                    # Project-root scripts (code_tests, bump_version, pre-commit)
docker-compose.yml          # Development service
pyproject.toml              # Project metadata, dependencies, ruff config
```

## Code Style

- Python 3.11+, async/await with Tornado and uvloop
- Ruff linter: line-length 120, rules E/F/I/B/UP
- Double quotes, space indentation
- Type hints on all function signatures
- Always use braces/blocks for conditionals (no single-line ifs)
- File-scoped imports only — never inside functions
