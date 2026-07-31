import asyncio
import datetime

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.migrations.discovery import MigrationError
from py_app_runner.migrations.tracker import Tracker
from tests.migrations_pg import dsn, pg_dsn_for


@pytest_asyncio.fixture
async def conn():
    async with pg_dsn_for("par_test_tracker") as db_dsn:
        async with await psycopg.AsyncConnection.connect(db_dsn, autocommit=True) as connection:
            yield connection


class TestEnsureTable:
    async def test_creates_the_table(self, conn):
        await Tracker(conn).ensure_table()

        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.migrations')")
            assert (await cur.fetchone())[0] == "migrations"

    async def test_is_idempotent(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()
        await tracker.ensure_table()

        assert await tracker.applied_rows() == []

    async def test_honours_a_custom_table_name(self, conn):
        await Tracker(conn, table="schema_history").ensure_table()

        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.schema_history')")
            assert (await cur.fetchone())[0] == "schema_history"

    async def test_refuses_to_adopt_an_unrelated_table_of_the_same_name(self, conn):
        """`CREATE TABLE IF NOT EXISTS` matches on name only, and `migrations` is a generic
        name. Adopting someone else's table silently made every later query die with a bare
        `column "name" does not exist`, which points at nothing."""

        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE migrations (id serial primary key, version int)")

        with pytest.raises(MigrationError) as excinfo:
            await Tracker(conn).ensure_table()

        message = str(excinfo.value)
        assert "migrations" in message
        assert "name" in message  # the missing column is named
        assert "version" in message  # so is what was actually found
        assert 'config["migrations"]["table"]' in message  # and the way out

    async def test_a_table_with_extra_columns_is_still_accepted(self, conn):
        """Only missing columns are a problem; an operator's own added column is not."""

        tracker = Tracker(conn)
        await tracker.ensure_table()
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE migrations ADD COLUMN note TEXT")

        await tracker.ensure_table()

    async def test_a_same_named_table_in_another_schema_does_not_answer_for_it(self, conn):
        """The column check resolves the schema from the oid search_path actually picks, so a
        decoy `other.migrations` cannot vouch for a broken `public.migrations`."""

        async with conn.cursor() as cur:
            await cur.execute("CREATE SCHEMA other")
            await cur.execute(
                "CREATE TABLE other.migrations ("
                "id int, name text, checksum text, applied_at timestamptz, duration_ms int, applied_by text)"
            )
            await cur.execute("CREATE TABLE public.migrations (id serial primary key, version int)")

        with pytest.raises(MigrationError):
            await Tracker(conn).ensure_table()


class TestRows:
    async def test_record_then_read_back(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()
        await tracker.record("2026-08-04-091530-a.sql", "abc", 12, "0.3 @ host")

        rows = await tracker.applied_rows()
        assert len(rows) == 1
        assert rows[0].name == "2026-08-04-091530-a.sql"
        assert rows[0].checksum == "abc"
        assert isinstance(rows[0].applied_at, datetime.datetime)

    async def test_rows_come_back_ordered_by_name(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()
        await tracker.record("2026-08-05-091530-b.sql", "b", 1, "x")
        await tracker.record("2026-07-29-000001-a.sql", "a", 1, "x")

        assert [r.name for r in await tracker.applied_rows()] == [
            "2026-07-29-000001-a.sql",
            "2026-08-05-091530-b.sql",
        ]

    async def test_recording_the_same_name_twice_is_rejected(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()
        await tracker.record("2026-08-04-091530-a.sql", "abc", 1, "x")

        with pytest.raises(psycopg.errors.UniqueViolation):
            await tracker.record("2026-08-04-091530-a.sql", "abc", 1, "x")

    async def test_update_checksum_reports_whether_a_row_matched(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()
        await tracker.record("2026-08-04-091530-a.sql", "old", 1, "x")

        assert await tracker.update_checksum("2026-08-04-091530-a.sql", "new") is True
        assert (await tracker.applied_rows())[0].checksum == "new"
        assert await tracker.update_checksum("2026-08-04-091530-nope.sql", "new") is False


class TestLock:
    async def test_second_holder_waits_for_the_first(self, conn):
        """Two connections, one lock. The second must not proceed until the first releases -
        this is what makes a future container-entrypoint auto-apply safe."""

        await Tracker(conn).ensure_table()
        order: list[str] = []
        # conn.info.dsn may omit the password, so build the second connection's DSN from the
        # shared test helper instead of trusting what psycopg reports back.
        second_dsn = dsn(conn.info.dbname)

        async def first():
            async with Tracker(conn).lock():
                order.append("first-acquired")
                await asyncio.sleep(0.4)
                order.append("first-released")

        async def second():
            await asyncio.sleep(0.1)
            async with await psycopg.AsyncConnection.connect(second_dsn, autocommit=True) as other:
                async with Tracker(other).lock():
                    order.append("second-acquired")

        await asyncio.gather(first(), second())

        assert order == ["first-acquired", "first-released", "second-acquired"]

    async def test_lock_is_released_when_the_body_raises(self, conn):
        tracker = Tracker(conn)
        await tracker.ensure_table()

        with pytest.raises(RuntimeError):
            async with tracker.lock():
                raise RuntimeError("boom")

        async with conn.cursor() as cur:
            # Advisory locks are cluster-wide, not per-database - scope to this backend so
            # another connection's unrelated lock cannot make this assertion pass by accident.
            await cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            assert (await cur.fetchone())[0] == 0

    async def test_original_error_survives_a_failed_transaction_on_release(self, conn):
        """On a non-autocommit connection, a real SQL error inside the lock body leaves the
        session in an aborted transaction. Releasing the lock must roll that back before
        unlocking, so the caller sees the original SQL error rather than an
        InFailedSqlTransaction raised by the unlock itself."""

        await Tracker(conn).ensure_table()

        other_dsn = dsn(conn.info.dbname)
        async with await psycopg.AsyncConnection.connect(other_dsn, autocommit=False) as other:
            tracker = Tracker(other)

            with pytest.raises(psycopg.errors.UndefinedTable):
                async with tracker.lock():
                    async with other.cursor() as cur:
                        await cur.execute("SELECT * FROM this_table_does_not_exist")

            async with other.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                )
                assert (await cur.fetchone())[0] == 0
