import contextlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from database_wrapper_sqlite import SqliteConnectionAsync

from py_app_runner import file_lock
from py_app_runner.migrations.discovery import MigrationError
from py_app_runner.migrations.states import AppliedRow
from py_app_runner.migrations.tracker import check_columns
from py_app_runner.utils import run_blocking

_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqliteTracker:
    """Tracker's SQLite twin. SQLite has no advisory lock, so `apply` serialises on a flock
    of a file beside the database instead."""

    def __init__(self, conn: SqliteConnectionAsync, table: str = "migrations") -> None:
        if not _TABLE_NAME.match(table):
            raise MigrationError(f"Tracking table name {table!r} must be a plain identifier")

        self.conn = conn
        self.table_name = table
        self.table = f'"{table}"'

    async def ensure_table(self) -> None:
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                checksum    TEXT NOT NULL,
                applied_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                duration_ms INTEGER NOT NULL,
                applied_by  TEXT NOT NULL
            )
            """
        )

        cur = await self.conn.execute(f"PRAGMA table_info({self.table})")
        check_columns(self.table_name, {row["name"] for row in await cur.fetchall()})

    async def applied_rows(self) -> list[AppliedRow]:
        cur = await self.conn.execute(f"SELECT name, checksum, applied_at FROM {self.table} ORDER BY name")
        return [
            AppliedRow(
                name=row["name"],
                checksum=row["checksum"],
                # CURRENT_TIMESTAMP is UTC, stored as text without a zone.
                applied_at=datetime.fromisoformat(row["applied_at"]).replace(tzinfo=UTC),
            )
            for row in await cur.fetchall()
        ]

    async def record(self, name: str, checksum: str, duration_ms: int, applied_by: str) -> None:
        await self.conn.execute(
            f"INSERT INTO {self.table} (name, checksum, duration_ms, applied_by) VALUES (?, ?, ?, ?)",
            (name, checksum, duration_ms, applied_by),
        )

    async def update_checksum(self, name: str, checksum: str) -> bool:
        cur = await self.conn.execute(f"UPDATE {self.table} SET checksum = ? WHERE name = ?", (checksum, name))
        return cur.rowcount == 1

    @contextlib.asynccontextmanager
    async def lock(self) -> AsyncIterator[None]:
        if self.conn.database in ("", ":memory:"):
            yield
            return

        fd = await run_blocking(file_lock.lock, f"{self.conn.database}.migrations.lock")
        try:
            yield
        finally:
            file_lock.unlock(fd)
