# py_app_runner

Async Python service framework: Tornado HTTP/WS bridge, PyBridge service loader, Redis WebSocket connection manager.

## Install

```bash
pip install py_app_runner
```

Every push to `develop` publishes a pre-release, `X.Y.<commit count>.dev0`. pip hides those
unless you ask for them:

```bash
pip install --pre py_app_runner        # newest, including dev builds
pip install py_app_runner==0.4.50.dev0 # a specific dev build
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

Each target's `db` names a key under `config["db"]` and defaults to the target's own name.
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

## Scheduled jobs

Built-in service that runs `app.py` subcommands on a cron schedule declared in config, the
way Laravel's scheduler does: the system crontab calls `cron run` once a minute and it
starts whatever is due. Add `cron` to `SERVICES` to enable it.

```bash
python3 src/app.py cron list                       # every job, its schedule and next run
python3 src/app.py cron run  [--job NAME] [--dry-run]
python3 src/app.py cron work                       # the same, looping, for a container without a crontab
```

```python
"cron": {
    "timezone": "Europe/Riga",
    "jobs": {
        "lad-sync": {"schedule": "0 4 * * 0", "command": "parcel lad sync"},
        "cleanup":  {"schedule": "15 4 * * *", "command": "cron cleanup", "timeout": 1800},
    },
}
```

```
* * * * * app cd /srv/app && python3 src/app.py cron run 2>&1 | logger -t cron
```

Each job runs as its own `app.py` process, so a wedged or leaking job takes only itself
down and `timeout` can kill it. A Postgres advisory lock per job stops a tick from starting
a second copy of one still running. There is no run history and no catch-up: a job is due
when its expression matches the current minute, and a missed minute is missed.
