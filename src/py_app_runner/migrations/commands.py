import pathlib
import time
from collections.abc import Callable

import psycopg

from py_app_runner.migrations.discovery import MigrationError, discover, find_meta_commands
from py_app_runner.migrations.states import MigrationState, State, blocking, compute_states, pending
from py_app_runner.migrations.tracker import Tracker

Out = Callable[[str], None]

# Length of the `YYYY-MM-DD-HHMMSS` prefix that discovery.MIGRATION_FILENAME_RE matches as
# its first group. Sliced off `state.name` rather than read from `MigrationFile.prefix`,
# because MISSING states have `file is None` - the name string is the only thing every
# state is guaranteed to have.
PREFIX_LENGTH = 17


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
            known = {state.name[:PREFIX_LENGTH] for state in states}
            if to not in known:
                out(f"error: no migration with prefix {to!r}")
                return 1

            queue = [state for state in queue if state.name[:PREFIX_LENGTH] <= to]

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
                        await cur.execute(state.file.sql.encode())

                    duration_ms = int((time.monotonic() - started) * 1000)
                    await tracker.record(state.name, state.file.checksum, duration_ms, applied_by)
                else:
                    # The tracking row is written inside the same transaction as the
                    # migration, so a file either fully lands and is recorded, or neither.
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(state.file.sql.encode())

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
