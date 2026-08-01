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

Built-in service that applies tracked SQL files to one or more configured databases. Add
`migrations` to `SERVICES` to enable it.

```bash
python3 src/app.py migrations status   [--check] [--target NAME]
python3 src/app.py migrations apply    [--dry-run] [--to PREFIX] [--target NAME]
python3 src/app.py migrations baseline [--to PREFIX] [--yes] [--target NAME]
python3 src/app.py migrations new      <name> [--target NAME]
python3 src/app.py migrations repair   <filename> [--target NAME]
```

By default there is a single target, `main`, against `config["db"]["main"]`: files live in
`config["migrations"]["dir"]` (default `data/migrations`) and are tracked in
`config["migrations"]["table"]` (default `migrations`). **No project needs to change
anything to keep this working** - an absent `migrations` key, an empty one, and this flat
shape all resolve to that same single target.

To migrate more than one database, opt in with `config["migrations"]["targets"]`:

```python
"migrations": {
    "targets": {
        "main": {"db": "main", "dir": "data/migrations", "table": "migrations"},
        "gis": {"db": "gis", "dir": "data/migrations_gis", "table": "gis_migrations"},
    }
}
```

Each target's `db` names a key under `config["db"]` and defaults to the target's own name;
`dir` and `table` default as above. `status` and `apply` with no `--target` run every
target in declared order (`apply` stops at the first one that fails; `status --check`
reports each target rather than stopping at the first with pending work, and exits 1 if any
is pending or blocked). `new`, `repair` and `baseline` require `--target` once more than one
target is configured, and an unknown `--target` exits 1 - both name the configured targets.
Output gets a `[name] ` prefix only when more than one target is processed, so single-target
output is unchanged.

Targets may share a database, but not a database *and* a tracking table - each would then
report the other's migrations as missing, so that config is refused up front, naming both
targets. `table` defaults to `migrations` for every target, so two targets on one database
need an explicit `table` on at least one of them.

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
- Every subcommand exits non-zero on failure, including a misconfigured `targets` block.
  `status --check` exits 1 if anything is pending, drifted or missing, so a deploy script
  can halt before restarting services against a half-migrated database.
