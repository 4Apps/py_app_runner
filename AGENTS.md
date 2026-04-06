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
```

## Architecture

### Core Components

- **`runner.py`** — CLI entrypoint (`main()`). Parses args, sets up logging/Sentry, loads services via PyBridge, starts the async event loop (uvloop in prod).
- **`pybridge.py`** — Dynamic service loader. Imports service modules from `services.<name>._service` / `_service_args` / `_service_pybridge` convention.
- **`registry.py`** — `AppRegistry` singleton. Holds project-specific config, model classes, audit log fn, Redis channel, and web app class. Must be configured before runner starts.
- **`config.py`** — `load_config()` loads `.env`, maps `ENV_VAR` names into nested dict keys via `replace_rec()`. Helpers: `is_env_dev/test/prod`.
- **`db_pools.py`** — Database connection pool management.

### Bridge Subsystem (`bridge/`)

Tornado-based HTTP and WebSocket server:
- **`web_app.py`** — Tornado web application setup.
- **`api.py`** — API request handling.
- **`websocket.py`** — WebSocket endpoint handler.
- **`encoders/`** — Pluggable encoders (JSON via msgspec, MessagePack).
- **`_service.py` / `_service_args.py`** — Bridge as a loadable PyBridge service.

### Request Handler (`request_handler/`)

- **`handlers.py`** — Base request handlers.
- **`decorators.py`** — Route/auth decorators.
- **`auth_service.py`** — JWT-based authentication.
- **`pagination.py`** — Pagination utilities.

### WebSocket Connection Manager (`wbcm/`)

Redis-backed WebSocket connection manager for multi-instance pub/sub:
- **`wb_connection_manager.py`** — Core manager.
- **`device_connections.py`** — Per-device connection tracking.
- **`factory.py`** — Factory for creating manager instances.
- **`ws_interface.py`** — WebSocket interface abstraction.

### Supporting Modules

- **`http_exception.py`** — HTTP exception with status codes.
- **`return_model.py`** — Standardized API response model.
- **`tick_service.py`** — Periodic tick service base.
- **`timer.py`** — Timer utilities.
- **`colors.py`** — Terminal color constants.
- **`utils.py`** — Shared helpers (JSON encoding, etc.).
- **`logger_handlers.py`** — Console log handler + Sentry init.

## Key Paths

```
src/py_app_runner/          # Package source
docker/app/Dockerfile       # Multi-stage Dockerfile (base -> builder -> development)
docker/app/scripts/         # Container scripts (code_tests, console helpers, bashrc)
docker-compose.yml          # Development service
pyproject.toml              # Project metadata, dependencies, ruff config
```

## Code Style

- Python 3.11+, async/await with Tornado and uvloop
- Ruff linter: line-length 120, rules E/F/I/B/UP
- Double quotes, space indentation
- Type hints on all function signatures
- Always use braces/blocks for conditionals (no single-line ifs)
