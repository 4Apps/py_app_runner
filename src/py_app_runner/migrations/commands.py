import pathlib
import time
from collections.abc import Callable
from datetime import datetime

import psycopg

from py_app_runner.migrations.discovery import (
    MigrationError,
    discover,
    find_meta_commands,
    load_migration,
    new_filename,
)
from py_app_runner.migrations.states import MigrationState, State, blocking, compute_states, pending
from py_app_runner.migrations.tracker import Tracker

Out = Callable[[str], None]


async def _load_states(
    conn: psycopg.AsyncConnection, directory: pathlib.Path, table: str
) -> tuple[Tracker, list[MigrationState]]:
    tracker = Tracker(conn, table)
    await tracker.ensure_table()
    files = discover(directory)
    rows = await tracker.applied_rows()

    return tracker, compute_states(files, rows)


def _report_blocking(states: list[MigrationState], out: Out) -> None:
    for state in blocking(states):
        if state.state is State.DRIFT:
            out(f"  {state.state:<8} {state.name}  (file changed after it was applied)")
        else:
            out(f"  {state.state:<8} {state.name}  (applied, but the file is gone)")

    out("")
    out("Resolve these before applying: restore the file, revert the edit, or run")
    out("`migrations repair <name>` if the edit was deliberate.")


async def cmd_status(
    conn: psycopg.AsyncConnection,
    directory: pathlib.Path,
    table: str,
    check: bool,
    out: Out,
) -> int:
    try:
        _tracker, states = await _load_states(conn, directory, table)
    except MigrationError as e:
        out(f"error: {e}")
        return 1

    if not states:
        out("No migrations found.")
        return 0

    for state in states:
        applied_at = state.row.applied_at.strftime("%Y-%m-%d %H:%M") if state.row else ""
        out(f"  {state.state:<8} {state.name:<52} {applied_at}")

    blocked = blocking(states)
    waiting = pending(states)

    out("")
    out(f"{len(states) - len(waiting) - len(blocked)} applied, {len(waiting)} pending, {len(blocked)} blocked")

    if check and (waiting or blocked):
        return 1

    return 0


async def cmd_apply(
    conn: psycopg.AsyncConnection,
    directory: pathlib.Path,
    table: str,
    dry_run: bool,
    to: str | None,
    applied_by: str,
    out: Out,
) -> int:
    """Apply pending migrations to `conn`.

    `conn` must be in autocommit mode. `tracker.lock()`'s advisory-lock statement would
    otherwise implicitly open a transaction, so each migration's `conn.transaction()`
    degrades to a SAVEPOINT instead of a real BEGIN/COMMIT - a mid-run failure would then
    roll the whole outer transaction back on unlock, undoing migrations this function
    already reported as applied. `CREATE INDEX CONCURRENTLY` in the no-transaction branch
    also requires autocommit outright.
    """

    if not conn.autocommit:
        out("error: cmd_apply requires an autocommit connection")
        return 1

    tracker = Tracker(conn, table)

    async with tracker.lock():
        try:
            _tracker, states = await _load_states(conn, directory, table)
        except MigrationError as e:
            out(f"error: {e}")
            return 1

        if blocking(states):
            _report_blocking(states, out)
            return 1

        queue = pending(states)
        if to is not None:
            # blocking(states) was empty, so no MISSING state remains here and every
            # state has a file - the `is not None` filter is structural narrowing for
            # pyrefly, not a real behavioural filter.
            known = {state.file.prefix for state in states if state.file is not None}
            if to not in known:
                out(f"error: no migration with prefix {to!r}")
                return 1

            queue = [state for state in queue if state.file is not None and state.file.prefix <= to]

        if not queue:
            out("Database is up to date; nothing to apply.")
            return 0

        for state in queue:
            assert state.file is not None
            meta = find_meta_commands(state.file.sql)
            if meta:
                out(f"error: {state.name} contains psql meta-command(s) psycopg cannot execute:")
                for line_number, line in meta:
                    out(f"    line {line_number}: {line}")

                out("Strip them (pg_dump emits \\restrict / \\unrestrict) and try again.")
                return 1

        for state in queue:
            assert state.file is not None
            if dry_run:
                out(f"would apply {state.name}")
                continue

            started = time.monotonic()
            duration_ms = 0
            try:
                if state.file.no_transaction:
                    async with conn.cursor() as cur:
                        await cur.execute(state.file.sql.encode(conn.info.encoding))

                    duration_ms = int((time.monotonic() - started) * 1000)
                    await tracker.record(state.name, state.file.checksum, duration_ms, applied_by)
                else:
                    # The tracking row is written inside the same transaction as the
                    # migration, so a file either fully lands and is recorded, or neither.
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(state.file.sql.encode(conn.info.encoding))

                        duration_ms = int((time.monotonic() - started) * 1000)
                        await tracker.record(state.name, state.file.checksum, duration_ms, applied_by)

            except Exception as e:
                out(f"FAILED {state.name}: {e}")
                out("Stopped. Nothing after this migration was applied.")
                return 1

            out(f"applied {state.name} ({duration_ms} ms)")

        if dry_run:
            out(f"{len(queue)} migration(s) would be applied; nothing was changed.")
        else:
            out(f"Applied {len(queue)} migration(s).")

        return 0


_NEW_FILE_TEMPLATE = """-- {name}
--
-- Runs in a transaction. Add `-- migrations:no-transaction` as the very first line if this
-- file needs CREATE INDEX CONCURRENTLY or anything else Postgres refuses inside one.
"""


async def cmd_baseline(
    conn: psycopg.AsyncConnection,
    directory: pathlib.Path,
    table: str,
    to: str | None,
    assume_yes: bool,
    applied_by: str,
    prompt: Callable[[str], str],
    out: Out,
) -> int:
    """Writes tracking rows without executing anything - the adoption path for a database
    that already has the schema. Nothing is written until every answer is in, so `q` really
    does leave the table untouched.

    `conn` must be in autocommit mode, for the same reason as `cmd_apply`: `tracker.record()`
    issues its own INSERT per row with no wrapping transaction and no explicit commit, so on a
    non-autocommit connection those rows would sit uncommitted - silently discarded if the
    caller never commits, rather than genuinely "written".
    """

    if not conn.autocommit:
        out("error: cmd_baseline requires an autocommit connection")
        return 1

    tracker = Tracker(conn, table)

    async with tracker.lock():
        try:
            _tracker, states = await _load_states(conn, directory, table)
        except MigrationError as e:
            out(f"error: {e}")
            return 1

        candidates = pending(states)
        if to is not None:
            # Unlike cmd_apply, this runs without a blocking() guard in front of it, so a
            # MISSING state (file is None) can still be in `states` here.
            known = {state.file.prefix for state in states if state.file is not None}
            if to not in known:
                out(f"error: no migration with prefix {to!r}")
                return 1

            candidates = [state for state in candidates if state.file is not None and state.file.prefix <= to]

        if not candidates:
            out("Nothing to baseline; every migration on disk is already recorded.")
            return 0

        chosen = []
        stamp_rest = assume_yes
        for state in candidates:
            if stamp_rest:
                chosen.append(state)
                continue

            answer = prompt(f"  {state.name:<52} mark as already applied? [y/N/a/q] ").strip().lower()
            if answer == "q":
                out("Aborted; nothing was written.")
                return 1

            if answer == "a":
                stamp_rest = True
                chosen.append(state)
            elif answer == "y":
                chosen.append(state)

        for state in chosen:
            assert state.file is not None
            await tracker.record(state.name, state.file.checksum, 0, applied_by)

        out("")
        out(
            f"stamped {len(chosen)} migration(s) as applied (not executed); "
            f"{len(candidates) - len(chosen)} left pending"
        )

        return 0


def cmd_new(directory: pathlib.Path, name: str, now: datetime, out: Out) -> int:
    try:
        filename = new_filename(name, now)
    except MigrationError as e:
        out(f"error: {e}")
        return 1

    if not directory.is_dir():
        out(f"error: migrations directory does not exist: {directory}")
        return 1

    path = directory / filename
    if path.exists():
        out(f"error: {path} already exists")
        return 1

    path.write_text(_NEW_FILE_TEMPLATE.format(name=name))
    out(f"created {path}")

    return 0


async def cmd_repair(
    conn: psycopg.AsyncConnection,
    directory: pathlib.Path,
    table: str,
    name: str,
    out: Out,
) -> int:
    """Re-stamps one migration's checksum after a deliberate edit. The only way out of DRIFT
    short of reverting the file.

    `conn` must be in autocommit mode, for the same reason as `cmd_baseline`: `update_checksum`
    is a single UPDATE with no explicit commit of its own.
    """

    if not conn.autocommit:
        out("error: cmd_repair requires an autocommit connection")
        return 1

    path = directory / name
    if not path.is_file():
        out(f"error: no such migration file: {path}")
        return 1

    try:
        migration = load_migration(path)
    except MigrationError as e:
        out(f"error: {e}")
        return 1

    tracker = Tracker(conn, table)
    await tracker.ensure_table()

    if not await tracker.update_checksum(migration.name, migration.checksum):
        out(f"error: {migration.name} has no tracking row - it was never applied, so there is nothing to repair")
        return 1

    out(f"repaired {migration.name}: checksum re-stamped to {migration.checksum[:12]}...")

    return 0
