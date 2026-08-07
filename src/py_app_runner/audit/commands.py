"""Command implementations, importable directly for programmatic use."""

import datetime
import pathlib
import re
from collections.abc import Callable

import psycopg
from psycopg import sql

from py_app_runner.audit.errors import AuditError
from py_app_runner.audit.store import assert_table_name, qualified
from py_app_runner.migrations.discovery import MigrationError, new_filename

Out = Callable[[str], None]

_TEMPLATE = pathlib.Path(__file__).parent / "files" / "install.pgsql.sql"

# No relative forms. "yesterday" in a retention job is a question about whose clock, and
# the answer only ever surfaces once rows are gone.
_BEFORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")


def cmd_install(migrations_dir: pathlib.Path, table: str, now: datetime.datetime, out: Out) -> int:
    """Write the schema into the project's own migrations directory.

    The framework ships no migration of its own and never creates the table at runtime:
    migrations are discovered by filename order and checksummed once applied, so a
    framework-owned file would sort by dependency release date and turn every upgrade into
    checksum drift on a file the application cannot edit.
    """

    try:
        assert_table_name(table)
    except AuditError as e:
        out(f"error: {e}")
        return 2

    if not migrations_dir.is_dir():
        out(f"error: no migrations directory at {migrations_dir}")
        return 2

    try:
        schema = _TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        out(f"error: could not read {_TEMPLATE}: {e}")
        return 1

    if table != "audit_log":
        # Renames the indexes along with the table, so two trails can coexist in one schema
        # without their index names colliding.
        schema = schema.replace("audit_log", table)

    try:
        target = migrations_dir / new_filename(f"create {table}", now)
    except MigrationError as e:
        out(f"error: {e}")
        return 2

    if target.exists():
        out(f"error: {target} already exists")
        return 1

    try:
        target.write_text(schema, encoding="utf-8")
    except OSError as e:
        out(f"error: could not write {target}: {e}")
        return 1

    out(f"Wrote {target}")
    out("Review it, then: python3 src/app.py migrations apply")

    return 0


async def cmd_prune(
    conn: psycopg.AsyncConnection,
    table: str,
    before: str,
    batch: int,
    dry_run: bool,
    out: Out,
) -> int:
    """Delete trail rows older than a date, in batches.

    Batched because a single DELETE over a year of a busy trail takes a lock long enough to
    be noticed, and because an interrupted run should leave the work it already did done.
    """

    try:
        assert_table_name(table)
    except AuditError as e:
        out(f"error: {e}")
        return 2

    if _BEFORE_RE.match(before) is None:
        out(f"error: --before={before!r} must be YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'")
        return 2

    if batch < 1:
        out("error: --batch must be at least 1")
        return 2

    relation = qualified(table)

    try:
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("SELECT count(*) FROM {rel} WHERE created_at < %s").format(rel=relation),
                (before,),
            )
            row = await cur.fetchone()
            total = row[0] if row else 0
    except psycopg.Error as e:
        out(f"error: cannot read {table}: {e}")
        return 1

    out(f"{total} rows in {table} older than {before}")

    if total == 0:
        return 0

    if dry_run:
        out("Nothing deleted (--dry-run).")
        return 0

    statement = sql.SQL(
        "DELETE FROM {rel} WHERE id IN (SELECT id FROM {rel} WHERE created_at < %s ORDER BY id LIMIT %s)"
    ).format(rel=relation)

    deleted = 0
    while deleted < total:
        try:
            async with conn.cursor() as cur:
                await cur.execute(statement, (before, batch))
                removed = cur.rowcount
        except psycopg.Error as e:
            out(f"error: cannot delete from {table}: {e}")
            return 1

        if removed <= 0:
            # Nothing left to take. Breaking rather than looping keeps a miscounted total
            # from spinning forever.
            break

        deleted += removed
        out(f"Deleted {deleted}/{total}")

    out(f"Done. {deleted} rows removed.")

    return 0
