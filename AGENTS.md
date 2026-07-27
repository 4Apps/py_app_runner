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
- **`pybridge.py`** — Dynamic service loader. Imports service modules from `services.<name>._service` / `_service_args` / `_service_pybridge` convention.
- **`registry.py`** — `AppRegistry` singleton. Holds project-specific config, model classes, Redis channel, and web app class. Must be configured before runner starts.
- **`config.py`** — `load_config()` loads `.env`, maps `ENV_VAR` names into nested dict keys by splitting on `_`. Only vars whose first key exists in defaults are processed. Helpers: `is_env_dev/test/prod`.
- **`db_pools.py`** — Database connection pool management (PostgreSQL + Redis).

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
- `@require_auth_for_actions` — class decorator, applies `@authenticated` to all @action methods

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
