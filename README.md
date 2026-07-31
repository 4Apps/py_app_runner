# py_app_runner

Async Python service framework: Tornado HTTP/WS bridge, PyBridge service loader, Redis WebSocket connection manager.

## Install

```bash
pip install git+ssh://git@github.com/4Apps/py_app_runner.git
```

## Development

```bash
docker-compose up develop
```

## Migrations

Built-in service that applies tracked SQL files to `config["db"]["main"]`. Add `migrations`
to `SERVICES` to enable it.

```bash
python3 src/app.py migrations status   [--check]
python3 src/app.py migrations apply    [--dry-run] [--to PREFIX]
python3 src/app.py migrations baseline [--to PREFIX] [--yes]
python3 src/app.py migrations new      <name>
python3 src/app.py migrations repair   <filename>
```

- Migrations no longer need to be idempotent - each file runs in its own transaction with
  its tracking row written inside it, so what already ran is always known.
- Editing an applied file is detected as drift and blocks `apply` until reverted or repaired.
- Files must not contain psql meta-commands (`\restrict` / `\unrestrict`, as emitted by
  `pg_dump`) - psycopg cannot execute them, and `apply` refuses such a file up front.
- `baseline` adopts an existing database into the system: it writes tracking rows without
  executing anything.
