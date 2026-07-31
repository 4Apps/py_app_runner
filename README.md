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

Files live in `config["migrations"]["dir"]` (default `data/migrations`) and are tracked in
`config["migrations"]["table"]` (default `migrations`).

- Migrations no longer need to be idempotent - each file runs in its own transaction with
  its tracking row written inside it, so what already ran is always known.
- `-- migrations:no-transaction` on line 1 runs a file outside a transaction (for
  `CREATE INDEX CONCURRENTLY` and friends). Such a file must contain **exactly one
  statement**: Postgres wraps a multi-statement send in an implicit transaction, which would
  defeat the directive, so `apply` refuses it up front. A no-transaction file that fails
  cannot roll back and is not recorded - `apply` says so and tells you to inspect the
  database before re-running.
- Editing an applied file is detected as drift, and a tracked file that has since been
  deleted shows as missing; both block `apply` until resolved. `repair` fixes drift only;
  a missing file is fixed by restoring it, or by deleting its tracking row by hand
  (`apply` prints the exact `DELETE` when it blocks).
- Files must not contain psql meta-commands (`\restrict` / `\unrestrict`, as emitted by
  `pg_dump`) - psycopg cannot execute them, and `apply` refuses such a file up front.
- `baseline` adopts an existing database into the system: it writes tracking rows without
  executing anything.
- Every subcommand exits non-zero on failure; `status --check` exits 1 if anything is
  pending, drifted or missing, so a deploy script can halt before restarting services
  against a half-migrated database.
