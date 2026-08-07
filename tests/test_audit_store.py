import datetime
import json
import pathlib

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.audit import Actor, Audit, AuditError, AuditEvent, request_context
from py_app_runner.audit.commands import cmd_install, cmd_prune
from tests.migrations_pg import pg_dsn_for

SCHEMA = pathlib.Path(__file__).parent.parent / "src" / "py_app_runner" / "audit" / "files" / "install.pgsql.sql"


@pytest_asyncio.fixture
async def db_dsn():
    """Yielded separately from the connection because the rollback test opens a second one,
    and psycopg's `info.dsn` deliberately omits the password."""

    async with pg_dsn_for("par_test_audit") as dsn:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            async with connection.cursor() as cur:
                await cur.execute(SCHEMA.read_text(encoding="utf-8"))
                await cur.execute(
                    "CREATE TABLE people ("
                    "  id serial PRIMARY KEY,"
                    "  name text,"
                    "  status text,"
                    "  password text,"
                    "  balance numeric(10,2)"
                    ")"
                )
        yield dsn


@pytest_asyncio.fixture
async def conn(db_dsn):
    async with await psycopg.AsyncConnection.connect(db_dsn, autocommit=True) as connection:
        yield connection


async def trail(conn) -> list[dict]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT * FROM audit_log ORDER BY id")
        names = [d.name for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in await cur.fetchall()]


class TestInsert:
    async def test_writes_the_row_and_records_it(self, conn):
        audit = Audit()

        async with conn.cursor() as cur:
            new_id = await audit.insert(cur, "people", {"name": "Anna"}, module="hr")

        rows = await trail(conn)
        assert len(rows) == 1
        assert rows[0]["event"] == "created"
        assert rows[0]["entity_type"] == "people"
        assert rows[0]["module"] == "hr"
        assert rows[0]["new_values"] == {"name": "Anna"}
        assert rows[0]["old_values"] is None
        assert new_id is not None

    async def test_entity_id_is_populated_from_returning(self, conn):
        """Reading the id back any other way means guessing which sequence to ask about, and
        the failure mode is an empty entity_id - which quietly empties the column
        idx_audit_log_entity exists to search."""

        audit = Audit()
        async with conn.cursor() as cur:
            new_id = await audit.insert(cur, "people", {"name": "Anna"})

        assert (await trail(conn))[0]["entity_id"] == str(new_id)


class TestUpdate:
    async def test_records_the_before_values_without_a_hand_written_fetch(self, conn):
        audit = Audit()

        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, status) VALUES ('Anna', 'active') RETURNING id")
            person_id = (await cur.fetchone())[0]

            await audit.update(cur, "people", {"status": "left"}, {"id": person_id})

        row = (await trail(conn))[0]
        assert row["event"] == "updated"
        assert row["old_values"] == {"status": "active"}
        assert row["new_values"] == {"status": "left"}

    async def test_a_no_op_update_records_nothing(self, conn):
        """Writing the same value back is not a change, and a trail full of them is one
        nobody reads."""

        audit = Audit()
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, status) VALUES ('Anna', 'active') RETURNING id")
            person_id = (await cur.fetchone())[0]

            await audit.update(cur, "people", {"status": "active"}, {"id": person_id})

        assert await trail(conn) == []

    async def test_a_numeric_column_read_back_as_decimal_is_not_a_false_change(self, conn):
        """psycopg returns numeric as Decimal. Comparing str() of it against the float the
        caller passed would report a change on every update that touched the column."""

        audit = Audit()
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, balance) VALUES ('Anna', 10.50) RETURNING id")
            person_id = (await cur.fetchone())[0]

            await audit.update(cur, "people", {"balance": 10.5}, {"id": person_id})

        assert await trail(conn) == []

    async def test_one_event_per_affected_row(self, conn):
        audit = Audit()
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, status) VALUES ('Anna', 'active'), ('Bo', 'active')")

            await audit.update(cur, "people", {"status": "left"}, {"status": "active"})

        rows = await trail(conn)
        assert len(rows) == 2
        assert {r["entity_id"] for r in rows} == {"1", "2"}


class TestDelete:
    async def test_records_the_whole_row_it_removed(self, conn):
        audit = Audit()
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, status) VALUES ('Anna', 'active') RETURNING id")
            person_id = (await cur.fetchone())[0]

            await audit.delete(cur, "people", {"id": person_id})

        row = (await trail(conn))[0]
        assert row["event"] == "deleted"
        assert row["old_values"]["name"] == "Anna"
        assert row["new_values"] is None

    async def test_refuses_an_empty_condition(self, conn):
        """An empty condition builds no WHERE at all, so it would delete the whole table and
        write an audit row for every one of them. Nothing below this call refuses it."""

        audit = Audit()
        async with conn.cursor() as cur:
            with pytest.raises(AuditError) as excinfo:
                await audit.delete(cur, "people", {})

        assert "every row" in str(excinfo.value)


class TestTransactionGuarantee:
    async def test_a_rolled_back_change_takes_its_audit_row_with_it(self, conn, db_dsn):
        """The single guarantee the whole design exists to preserve. Any move to write the
        trail from a background task gives this up, and the trail starts claiming changes
        that never committed."""

        audit = Audit()

        async with await psycopg.AsyncConnection.connect(db_dsn) as tx_conn:
            async with tx_conn.cursor() as cur:
                await audit.insert(cur, "people", {"name": "Anna"})
            await tx_conn.rollback()

        assert await trail(conn) == []

        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM people")
            assert (await cur.fetchone())[0] == 0


class TestRedaction:
    async def test_an_excluded_column_is_recorded_as_changed_but_not_shown(self, conn):
        audit = Audit(exclude={"people": ["password"]})

        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, password) VALUES ('Anna', 'old') RETURNING id")
            person_id = (await cur.fetchone())[0]

            await audit.update(cur, "people", {"password": "new"}, {"id": person_id})

        row = (await trail(conn))[0]
        assert row["new_values"] == {"password": "***"}
        assert "new" not in json.dumps(row["new_values"])

    async def test_a_malformed_exclude_block_is_refused_at_construction(self):
        """Redaction fails open by its nature: a lookup finding nothing to exclude simply
        excludes nothing, so a malformed block leaks exactly the values it was meant to
        withhold, silently."""

        with pytest.raises(AuditError) as excinfo:
            Audit(exclude={"people": "password"})

        assert "list of column names" in str(excinfo.value)


class TestLimits:
    async def test_refuses_to_audit_more_rows_than_configured(self, conn):
        """A mistyped condition matching the whole table must not become one write plus half
        a million audit rows. Checked before the write, so nothing happens at all."""

        audit = Audit(max_rows=2)
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO people (name, status) SELECT 'p', 'active' FROM generate_series(1,5)")

            with pytest.raises(AuditError) as excinfo:
                await audit.update(cur, "people", {"status": "left"}, {"status": "active"})

        assert "max_rows" in str(excinfo.value)

        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM people WHERE status = 'left'")
            assert (await cur.fetchone())[0] == 0

    async def test_a_string_max_rows_is_refused_rather_than_compared(self, conn):
        with pytest.raises(AuditError) as excinfo:
            Audit.from_config({"audit": {"max_rows": "1000"}})

        assert "must be an int" in str(excinfo.value)


class TestContext:
    async def test_the_request_context_fills_in_actor_and_request_id(self, conn):
        audit = Audit()

        with request_context(
            actor=Actor(type="user", id="7", name="Gints"),
            url="/people/42",
            ip_address="10.0.0.1",
            user_agent="test-agent",
        ) as context:
            async with conn.cursor() as cur:
                await audit.insert(cur, "people", {"name": "Anna"})

        row = (await trail(conn))[0]
        assert row["actor_type"] == "user"
        assert row["actor_id"] == "7"
        assert row["actor_name"] == "Gints"
        assert row["request_id"] == context.request_id
        assert row["url"] == "/people/42"
        assert row["ip_address"] == "10.0.0.1"

    async def test_an_explicit_actor_survives_the_ambient_one(self, conn):
        """An import recording who requested it, rather than whoever happens to be logged
        in, keeps what it passed."""

        audit = Audit()

        with request_context(actor=Actor(type="user", id="7", name="Gints")):
            async with conn.cursor() as cur:
                await audit.record(
                    cur,
                    AuditEvent(
                        event="imported",
                        entity_type="people",
                        actor_type="cron",
                        actor_id="nightly",
                        actor_name="Nightly import",
                    ),
                )

        row = (await trail(conn))[0]
        assert row["actor_type"] == "cron"
        assert row["actor_name"] == "Nightly import"

    async def test_the_context_does_not_leak_out_of_its_block(self, conn):
        audit = Audit()

        with request_context(actor=Actor(type="user", id="7")):
            pass

        async with conn.cursor() as cur:
            await audit.insert(cur, "people", {"name": "Anna"})

        assert (await trail(conn))[0]["actor_id"] == ""


class TestWidths:
    async def test_an_over_long_value_is_cut_rather_than_refused(self, conn):
        """An audit write that throws because somebody's name is long has turned the trail
        into an outage."""

        audit = Audit()

        with request_context(actor=Actor(type="user", id="7", name="N" * 300)):
            async with conn.cursor() as cur:
                await audit.insert(cur, "people", {"name": "Anna"})

        assert len((await trail(conn))[0]["actor_name"]) == 190


class TestTableGuard:
    async def test_refuses_a_table_name_that_is_not_a_plain_identifier(self):
        with pytest.raises(AuditError) as excinfo:
            Audit(table="audit_log; DROP TABLE people")

        assert "not a plain table name" in str(excinfo.value)


class TestCommands:
    def test_install_writes_a_migration(self, tmp_path):
        lines: list[str] = []
        code = cmd_install(tmp_path, "audit_log", datetime.datetime(2026, 8, 2, 1, 2, 3), lines.append)

        assert code == 0
        written = list(tmp_path.glob("*.sql"))
        assert len(written) == 1
        assert written[0].name == "2026-08-02-010203-create-audit-log.sql"
        assert "CREATE TABLE audit_log" in written[0].read_text()

    def test_install_renames_the_indexes_along_with_the_table(self, tmp_path):
        """Two trails must be able to coexist in one schema, which they cannot if both sets
        of indexes are called idx_audit_log_*."""

        lines: list[str] = []
        cmd_install(tmp_path, "hr_audit", datetime.datetime(2026, 8, 2, 1, 2, 3), lines.append)

        sql_text = next(tmp_path.glob("*.sql")).read_text()
        assert "CREATE TABLE hr_audit" in sql_text
        assert "idx_hr_audit_entity" in sql_text
        assert "audit_log" not in sql_text

    def test_install_refuses_a_bad_table_name(self, tmp_path):
        lines: list[str] = []
        code = cmd_install(tmp_path, "bad-name", datetime.datetime(2026, 8, 2), lines.append)

        assert code == 2
        assert any("not a plain table name" in line for line in lines)

    async def test_prune_deletes_only_rows_older_than_the_date(self, conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO audit_log (created_at, event, entity_type) VALUES "
                "('2020-01-01', 'created', 'people'), ('2030-01-01', 'created', 'people')"
            )

        lines: list[str] = []
        assert await cmd_prune(conn, "audit_log", "2026-01-01", 10, False, lines.append) == 0
        assert len(await trail(conn)) == 1

    async def test_prune_dry_run_changes_nothing(self, conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO audit_log (created_at, event, entity_type) VALUES ('2020-01-01', 'created', 'p')"
            )

        lines: list[str] = []
        assert await cmd_prune(conn, "audit_log", "2026-01-01", 10, True, lines.append) == 0
        assert len(await trail(conn)) == 1
        assert any("--dry-run" in line for line in lines)

    async def test_prune_refuses_a_relative_date(self, conn):
        """ "yesterday" in a retention job is a question about whose clock, and the answer
        only surfaces once rows are gone."""

        lines: list[str] = []
        assert await cmd_prune(conn, "audit_log", "yesterday", 10, False, lines.append) == 2
