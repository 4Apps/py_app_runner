from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg import sql

from py_app_runner.migrations.states import AppliedRow

# One fixed key for the whole system. Two processes applying migrations at once would
# interleave DDL and duplicate tracking rows, so `apply` serialises on this.
ADVISORY_LOCK_KEY = 4829173465872001


class Tracker:
    """Reads and writes the migration tracking table.

    The table bootstraps itself on every invocation rather than being migration zero -
    otherwise there is nothing to record the fact that it was created.
    """

    def __init__(self, conn: "psycopg.AsyncConnection", table: str = "migrations") -> None:
        self.conn = conn
        self.table = sql.Identifier(table)

    async def ensure_table(self) -> None:
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

    @asynccontextmanager
    async def lock(self) -> AsyncIterator[None]:
        """Session-level advisory lock held for the whole run. A crashed process drops its
        connection, and Postgres releases the lock with it."""

        async with self.conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))

        try:
            yield
        finally:
            async with self.conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
