import contextlib
from collections.abc import AsyncIterator

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from py_app_runner.migrations.discovery import MigrationError
from py_app_runner.migrations.states import AppliedRow

# One fixed key for the whole system. Two processes applying migrations at once would
# interleave DDL and duplicate tracking rows, so `apply` serialises on this.
ADVISORY_LOCK_KEY = 4829173465872001

EXPECTED_COLUMNS = frozenset({"id", "name", "checksum", "applied_at", "duration_ms", "applied_by"})

# The schema is resolved from the oid rather than matched on `table_name` alone, so a
# same-named table in another schema cannot answer for the one search_path actually picks.
_COLUMNS_SQL = """
    SELECT column_name
    FROM information_schema.columns
    WHERE (table_schema, table_name) = (
        SELECT n.nspname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = to_regclass(%s)
    )
"""


class Tracker:
    """Reads and writes the migration tracking table.

    The table bootstraps itself on every invocation rather than being migration zero -
    otherwise there is nothing to record the fact that it was created.
    """

    def __init__(self, conn: "psycopg.AsyncConnection", table: str = "migrations") -> None:
        self.conn = conn
        self.table_name = table
        self.table = sql.Identifier(table)

    async def ensure_table(self) -> None:
        await self._create_table()
        await self._verify_columns()

    async def _create_table(self) -> None:
        statement = sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {table} (
                id          INT8 GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                checksum    TEXT NOT NULL,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                duration_ms INT4 NOT NULL,
                applied_by  TEXT NOT NULL
            )
            """
        ).format(table=self.table)

        async with self.conn.cursor() as cur:
            await cur.execute(statement)

    async def _verify_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` matches on name alone, and `migrations` is about as
        generic a table name as exists. Without this check an unrelated pre-existing table is
        silently adopted and the next query fails with a bare `column "name" does not exist`,
        with nothing pointing at this tool - exactly when an operator is adopting a database.
        """

        async with self.conn.cursor() as cur:
            await cur.execute(_COLUMNS_SQL, (self.table.as_string(),))
            found = {row[0] for row in await cur.fetchall()}

        missing = EXPECTED_COLUMNS - found
        if not missing:
            return

        raise MigrationError(
            f"Table {self.table_name!r} exists but is not a migration tracking table: "
            f"missing column(s) {', '.join(sorted(missing))}; it has {', '.join(sorted(found)) or 'no columns'}. "
            f'Point the tracker at a free name via config["migrations"]["table"], '
            f"or drop/rename the existing table if it is not in use."
        )

    async def applied_rows(self) -> list[AppliedRow]:
        statement = sql.SQL("SELECT name, checksum, applied_at FROM {table} ORDER BY name").format(
            table=self.table
        )

        async with self.conn.cursor() as cur:
            await cur.execute(statement)
            return [AppliedRow(name=row[0], checksum=row[1], applied_at=row[2]) for row in await cur.fetchall()]

    async def record(self, name: str, checksum: str, duration_ms: int, applied_by: str) -> None:
        statement = sql.SQL(
            "INSERT INTO {table} (name, checksum, duration_ms, applied_by) VALUES (%s, %s, %s, %s)"
        ).format(table=self.table)

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (name, checksum, duration_ms, applied_by))

    async def update_checksum(self, name: str, checksum: str) -> bool:
        """False when no such row exists - the caller reports that rather than claiming a
        repair that never happened."""

        statement = sql.SQL("UPDATE {table} SET checksum = %s WHERE name = %s").format(table=self.table)

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (checksum, name))
            return cur.rowcount == 1

    @contextlib.asynccontextmanager
    async def lock(self) -> AsyncIterator[None]:
        """Session-level advisory lock held for the whole run. A crashed process drops its
        connection, and Postgres releases the lock with it.

        The lock will wrap real migration execution, so a failing body may leave a
        non-autocommit connection in an aborted transaction. Releasing the lock then needs a
        rollback first, and its own failure must never replace the body's real exception as
        what the caller sees.
        """

        async with self.conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))

        try:
            yield
        except BaseException:
            with contextlib.suppress(Exception):
                await self._unlock()
            raise
        else:
            await self._unlock()

    async def _unlock(self) -> None:
        if self.conn.info.transaction_status == TransactionStatus.INERROR:
            await self.conn.rollback()

        async with self.conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
