"""Command implementations, importable directly for programmatic use.

Each takes its collaborators explicitly and returns a process exit code rather than
printing and exiting, so the whole surface is testable without capturing stdout.
"""

import re
from collections.abc import Callable

import psycopg
from psycopg import sql

from py_app_runner.crypto.errors import CryptoError
from py_app_runner.crypto.fields import FieldCrypto, generate_key, key_id_of

Out = Callable[[str], None]

# The table and column reach SQL as identifiers, which cannot be bound as parameters. They
# go through psycopg's Identifier for quoting, but are whitelisted first: Identifier will
# happily quote `people; DROP TABLE people` into something valid-but-wrong, and a rotate
# run against a table nobody meant to name is not recoverable.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def cmd_key(out: Out) -> int:
    """Print fresh key material. Touches no config and no database, deliberately - the first
    key has to be generatable before there is a working install to generate it from."""

    out(generate_key())
    out("")
    out('Put it in the environment variable named by config["crypto"]["keys"],')
    out("not in a config file. Keep the previous key listed until:")
    out("  python3 src/app.py crypto rotate --table T --column C")
    out("reports nothing left under it.")

    return 0


def _qualified(table: str) -> sql.Composed:
    """Split an optional schema qualifier so each half is quoted separately.

    Identifier("public.people") quotes the dot into the name, producing "public.people" as
    a single relation that does not exist.
    """

    return sql.SQL(".").join(sql.Identifier(part) for part in table.split("."))


async def cmd_rotate(
    conn: psycopg.AsyncConnection,
    crypto: FieldCrypto,
    table: str,
    column: str,
    id_column: str,
    batch: int,
    dry_run: bool,
    out: Out,
) -> int:
    """Re-encrypt a column onto the current key.

    Idempotent: a second run costs a scan and changes nothing. Values already under the
    current key are skipped, and values that are not encrypted at all are left alone and
    counted, because a half-backfilled column is a normal state rather than an error.
    """

    if _TABLE_RE.match(table) is None:
        out(f"error: {table!r} is not a plain table name")
        return 2

    if _COLUMN_RE.match(column) is None:
        out(f"error: --column={column!r} is not a plain column name")
        return 2

    if _COLUMN_RE.match(id_column) is None:
        out(f"error: --id={id_column!r} is not a plain column name")
        return 2

    if batch < 1:
        out("error: --batch must be at least 1")
        return 2

    try:
        current = crypto.current_key_id()
    except CryptoError as e:
        out(f"error: {e}")
        return 1

    out(f"Rotating {table}.{column} onto key {current!r}" + (" (--dry-run)" if dry_run else ""))

    relation = _qualified(table)
    id_ident = sql.Identifier(id_column)
    column_ident = sql.Identifier(column)

    # The first page has no cursor. `id > NULL` is unknown rather than true, so it would
    # match no rows and the whole rotate would report an empty table; the guard makes the
    # predicate short-circuit instead of writing a second query for the first page.
    read = sql.SQL(
        "SELECT {id} AS id, {col} AS value FROM {rel} "
        "WHERE (%(cursor)s IS NULL OR {id} > %(cursor)s) ORDER BY {id} LIMIT %(batch)s"
    ).format(id=id_ident, col=column_ident, rel=relation)
    write = sql.SQL("UPDATE {rel} SET {col} = %s WHERE {id} = %s").format(rel=relation, col=column_ident, id=id_ident)

    cursor: object = None
    seen = 0
    rotated = 0
    plaintext = 0

    while True:
        try:
            async with conn.cursor() as cur:
                # Keyset pagination rather than OFFSET: OFFSET re-reads and discards every
                # row it skips, so the last page of a large table costs a full scan, and a
                # row updated mid-run shifts the window under it.
                await cur.execute(read, {"cursor": cursor, "batch": batch})
                rows = await cur.fetchall()
        except psycopg.Error as e:
            out(f"error: cannot read {table}: {e}")
            return 1

        if not rows:
            break

        for row_id, value in rows:
            seen += 1
            cursor = row_id

            if value is None or value == "":
                continue

            found = key_id_of(value)
            if found is None:
                plaintext += 1
                continue

            if found == current:
                continue

            rotated += 1
            if dry_run:
                continue

            try:
                fresh = crypto.encrypt(str(crypto.decrypt(value)), current)
            except CryptoError as e:
                out(f"error: {id_column} {row_id}: {e}")
                return 1

            try:
                async with conn.cursor() as cur:
                    await cur.execute(write, (fresh, row_id))
            except psycopg.Error as e:
                out(f"error: {id_column} {row_id}: {e}")
                return 1

        if len(rows) < batch:
            break

    out(f"Read {seen} rows.")
    if plaintext > 0:
        out(f"{plaintext} were not encrypted and were left alone.")
    out(f"{rotated} would be re-encrypted." if dry_run else f"{rotated} re-encrypted.")

    return 0
