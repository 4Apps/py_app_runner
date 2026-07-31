import datetime
import pathlib

import pytest
import pytest_asyncio

from py_app_runner.migrations.commands import cmd_apply, cmd_baseline, cmd_new, cmd_repair, cmd_status
from py_app_runner.migrations.tracker import Tracker
from tests.migrations_pg import dsn, pg_dsn_for

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


def scripted_prompt(answers: list[str]):
    """Feeds `baseline` a fixed sequence of answers, and blows up rather than hanging if it
    asks more questions than the test expects."""

    remaining = list(answers)

    def prompt(_question: str) -> str:
        if not remaining:
            raise AssertionError("baseline asked more questions than the test scripted")

        return remaining.pop(0)

    return prompt


class TestBaseline:
    async def test_stamps_the_answers_that_were_yes(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")

        code = await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["y", "n"]), out
        )

        assert code == 0
        assert [r.name for r in await Tracker(conn).applied_rows()] == ["2026-08-04-091530-a.sql"]

    async def test_never_executes_the_sql(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["y"]), out
        )

        assert not await table_exists(conn, "a")

    async def test_records_the_real_checksum_so_apply_sees_no_drift(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["y"]), out
        )
        lines.clear()

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert any("up to date" in line.lower() for line in lines)

    async def test_answering_a_stamps_everything_remaining(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")
        write(migrations_dir, "2026-08-06-091530-c.sql", "CREATE TABLE c (id int);")

        await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["a"]), out
        )

        assert len(await Tracker(conn).applied_rows()) == 3

    async def test_answering_q_writes_nothing_at_all(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")

        code = await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["y", "q"]), out
        )

        assert code == 1
        assert await Tracker(conn).applied_rows() == []

    async def test_empty_answer_defaults_to_no(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt([""]), out
        )

        assert await Tracker(conn).applied_rows() == []

    async def test_to_with_yes_is_non_interactive(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")

        code = await cmd_baseline(
            conn, migrations_dir, "migrations", "2026-08-04-091530", True, "test", scripted_prompt([]), out
        )

        assert code == 0
        assert [r.name for r in await Tracker(conn).applied_rows()] == ["2026-08-04-091530-a.sql"]

    async def test_already_applied_migrations_are_not_offered(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")

        # Only one question is scripted; a second would raise.
        code = await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["y"]), out
        )

        assert code == 0
        assert len(await Tracker(conn).applied_rows()) == 2

    async def test_write_step_is_all_or_nothing_when_a_write_fails_partway_through(
        self, conn, migrations_dir, printed, monkeypatch
    ):
        """The decide step (the `for state in candidates` prompt loop) already had this
        guarantee via the `q` path. This proves the separate write step (the `for state in
        chosen` loop that calls `tracker.record`) has it too: a failure on the second of
        three writes must not leave the first one committed."""

        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")
        write(migrations_dir, "2026-08-06-091530-c.sql", "CREATE TABLE c (id int);")

        original_record = Tracker.record
        calls = 0

        async def flaky_record(self, name, checksum, duration_ms, applied_by):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")

            return await original_record(self, name, checksum, duration_ms, applied_by)

        monkeypatch.setattr(Tracker, "record", flaky_record)

        code = await cmd_baseline(
            conn, migrations_dir, "migrations", None, False, "test", scripted_prompt(["a"]), out
        )

        assert code == 1
        assert await Tracker(conn).applied_rows() == []


class TestNew:
    def test_creates_a_timestamped_file(self, migrations_dir, printed):
        lines, out = printed
        now = datetime.datetime(2026, 8, 4, 9, 15, 30, tzinfo=datetime.UTC)

        assert cmd_new(migrations_dir, "add widgets table", now, out) == 0

        created = migrations_dir / "2026-08-04-091530-add-widgets-table.sql"
        assert created.exists()
        assert "add widgets table" in created.read_text()
        assert any(created.name in line for line in lines)

    def test_refuses_to_overwrite_an_existing_file(self, migrations_dir, printed):
        _lines, out = printed
        now = datetime.datetime(2026, 8, 4, 9, 15, 30, tzinfo=datetime.UTC)
        cmd_new(migrations_dir, "add widgets table", now, out)

        assert cmd_new(migrations_dir, "add widgets table", now, out) == 1


class TestRepair:
    async def test_restamps_a_drifted_migration(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- deliberate edit")

        assert await cmd_repair(conn, migrations_dir, "migrations", "2026-08-04-091530-a.sql", out) == 0

        lines.clear()
        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 0

    async def test_unknown_name_is_an_error(self, conn, migrations_dir, printed):
        _lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_repair(conn, migrations_dir, "migrations", "2026-08-04-091530-a.sql", out) == 1


class TestAutocommitGuard:
    """`cmd_apply`'s guard (added in Task 5) had no coverage; `cmd_baseline` and `cmd_repair`
    grow the same guard in this task, for the same reason: none of the three commit anything
    themselves, so a non-autocommit connection would leave their writes uncommitted rather than
    genuinely applied."""

    async def test_apply_refuses_a_non_autocommit_connection(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        other_dsn = dsn(conn.info.dbname)
        async with await psycopg.AsyncConnection.connect(other_dsn, autocommit=False) as other:
            code = await cmd_apply(other, migrations_dir, "migrations", False, None, "test", out)

        assert code == 1
        assert any("autocommit" in line.lower() for line in lines)
        assert not await table_exists(conn, "a")

    async def test_baseline_refuses_a_non_autocommit_connection(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        other_dsn = dsn(conn.info.dbname)
        async with await psycopg.AsyncConnection.connect(other_dsn, autocommit=False) as other:
            code = await cmd_baseline(
                other, migrations_dir, "migrations", None, False, "test", scripted_prompt([]), out
            )

        assert code == 1
        assert any("autocommit" in line.lower() for line in lines)

    async def test_repair_refuses_a_non_autocommit_connection(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- deliberate edit")

        other_dsn = dsn(conn.info.dbname)
        async with await psycopg.AsyncConnection.connect(other_dsn, autocommit=False) as other:
            code = await cmd_repair(other, migrations_dir, "migrations", "2026-08-04-091530-a.sql", out)

        assert code == 1
        assert any("autocommit" in line.lower() for line in lines)


class TestServiceConfig:
    def test_connect_kwargs_maps_pgsqlconfig_names(self):
        from py_app_runner.migrations._service import connect_kwargs

        assert connect_kwargs(
            {
                "hostname": "db.example.com",
                "port": 5433,
                "username": "app",
                "password": "secret",
                "database": "appdb",
                "ssl": "require",
            }
        ) == {
            "host": "db.example.com",
            "port": 5433,
            "user": "app",
            "password": "secret",
            "dbname": "appdb",
            "sslmode": "require",
            "autocommit": True,
        }

    def test_connect_kwargs_applies_defaults(self):
        from py_app_runner.migrations._service import connect_kwargs

        result = connect_kwargs(
            {"hostname": "db", "username": "app", "password": "s", "database": "appdb"}
        )

        assert result["port"] == 5432
        assert result["sslmode"] == "prefer"

    def test_settings_default_when_config_has_no_migrations_key(self, tmp_path):
        from py_app_runner.migrations._service import migrations_settings

        directory, table = migrations_settings({"current_path": str(tmp_path)})

        assert directory == tmp_path / "data" / "migrations"
        assert table == "migrations"

    def test_settings_honour_explicit_values(self, tmp_path):
        from py_app_runner.migrations._service import migrations_settings

        directory, table = migrations_settings(
            {"current_path": str(tmp_path), "migrations": {"dir": "sql/steps", "table": "schema_history"}}
        )

        assert directory == tmp_path / "sql" / "steps"
        assert table == "schema_history"

    def test_settings_accept_an_absolute_dir(self, tmp_path):
        from py_app_runner.migrations._service import migrations_settings

        directory, _table = migrations_settings(
            {"current_path": "/somewhere/else", "migrations": {"dir": str(tmp_path)}}
        )

        assert directory == tmp_path
