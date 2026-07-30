import pathlib

import pytest
import pytest_asyncio

from py_app_runner.migrations.commands import cmd_apply, cmd_status
from py_app_runner.migrations.tracker import Tracker
from tests.migrations_pg import pg_dsn_for

psycopg = pytest.importorskip("psycopg")


@pytest_asyncio.fixture
async def conn():
    async with pg_dsn_for("par_test_commands") as dsn:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            yield connection


@pytest.fixture
def migrations_dir(tmp_path) -> pathlib.Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


@pytest.fixture
def printed() -> tuple[list[str], object]:
    lines: list[str] = []
    return lines, lines.append


def write(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = directory / name
    path.write_text(body)
    return path


async def table_exists(conn, name: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
        return (await cur.fetchone())[0] is not None


class TestStatus:
    async def test_lists_pending_migrations_and_exits_zero(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_status(conn, migrations_dir, "migrations", check=False, out=out) == 0
        assert any("PENDING" in line and "2026-08-04-091530-a.sql" in line for line in lines)

    async def test_check_exits_one_when_something_is_pending(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 1

    async def test_check_exits_zero_when_everything_is_applied(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 0

    async def test_reports_drift(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- edited")

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 1
        assert any("DRIFT" in line for line in lines)

    async def test_reports_missing_when_an_applied_file_is_deleted(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.unlink()

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 1
        assert any("MISSING" in line for line in lines)

    async def test_creates_the_tracking_table_on_a_virgin_database(self, conn, migrations_dir, printed):
        _lines, out = printed

        await cmd_status(conn, migrations_dir, "migrations", check=False, out=out)

        assert await table_exists(conn, "migrations")


class TestApply:
    async def test_applies_pending_migrations_in_order(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int primary key);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (a_id int REFERENCES a (id));")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert await table_exists(conn, "a")
        assert await table_exists(conn, "b")

    async def test_running_twice_is_a_no_op(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        lines.clear()

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert any("up to date" in line.lower() for line in lines)

    async def test_multi_statement_files_are_supported(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "CREATE TABLE a (id int);\nCREATE TABLE b (id int);\nINSERT INTO a (id) VALUES (1);",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert await table_exists(conn, "b")

    async def test_dry_run_changes_nothing(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_apply(conn, migrations_dir, "migrations", True, None, "test", out) == 0
        assert not await table_exists(conn, "a")
        assert await Tracker(conn).applied_rows() == []
        assert any("would apply" in line.lower() for line in lines)

    async def test_to_stops_after_the_named_migration(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, "2026-08-04-091530", "test", out) == 0
        assert await table_exists(conn, "a")
        assert not await table_exists(conn, "b")

    async def test_unknown_to_prefix_is_an_error(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, "9999-01-01-000000", "test", out) == 1
        assert not await table_exists(conn, "a")

    async def test_a_failing_migration_rolls_back_and_records_nothing(self, conn, migrations_dir, printed):
        lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "CREATE TABLE a (id int);\nCREATE TABLE a (id int);",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert not await table_exists(conn, "a")
        assert await Tracker(conn).applied_rows() == []
        assert any("2026-08-04-091530-a.sql" in line for line in lines)

    async def test_stops_at_the_first_failure_leaving_later_files_pending(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "THIS IS NOT SQL;")
        write(migrations_dir, "2026-08-06-091530-c.sql", "CREATE TABLE c (id int);")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert await table_exists(conn, "a")
        assert not await table_exists(conn, "c")
        assert [r.name for r in await Tracker(conn).applied_rows()] == ["2026-08-04-091530-a.sql"]

    async def test_refuses_to_run_when_a_file_has_drifted(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- edited")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")
        lines.clear()

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert not await table_exists(conn, "b")
        assert any("DRIFT" in line for line in lines)

    async def test_refuses_files_containing_psql_meta_commands(self, conn, migrations_dir, printed):
        lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "\\restrict abc123\nCREATE TABLE a (id int);\n\\unrestrict abc123\n",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert not await table_exists(conn, "a")
        assert any("meta-command" in line for line in lines)

    async def test_no_transaction_directive_allows_create_index_concurrently(
        self, conn, migrations_dir, printed
    ):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(
            migrations_dir,
            "2026-08-05-091530-b.sql",
            "-- migrations:no-transaction\nCREATE INDEX CONCURRENTLY a_id_idx ON a (id);",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert [r.name for r in await Tracker(conn).applied_rows()] == [
            "2026-08-04-091530-a.sql",
            "2026-08-05-091530-b.sql",
        ]

    async def test_records_duration_and_who_applied_it(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "0.3 @ testhost", out)

        async with conn.cursor() as cur:
            await cur.execute("SELECT duration_ms, applied_by FROM migrations")
            duration_ms, applied_by = await cur.fetchone()

        assert duration_ms >= 0
        assert applied_by == "0.3 @ testhost"

    async def test_a_bad_filename_in_the_directory_is_reported_not_crashed(
        self, conn, migrations_dir, printed
    ):
        lines, out = printed
        write(migrations_dir, "026-add-widgets.sql", "CREATE TABLE a (id int);")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("026-add-widgets.sql" in line for line in lines)
