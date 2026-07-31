import datetime
import pathlib

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.migrations._service import Target, connect_kwargs, resolve_targets
from py_app_runner.migrations.commands import cmd_apply, cmd_baseline, cmd_new, cmd_repair, cmd_status
from py_app_runner.migrations.discovery import MigrationError
from py_app_runner.migrations.tracker import Tracker
from tests.migrations_pg import dsn, pg_dsn_for


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

    async def test_refuses_a_multi_statement_no_transaction_file(self, conn, migrations_dir, printed):
        """Postgres wraps a multi-statement simple-Query send in an implicit transaction, so
        the directive silently does not take effect - `CREATE INDEX CONCURRENTLY` then dies
        with "cannot run inside a transaction block". Refuse at scan time instead."""

        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        write(
            migrations_dir,
            "2026-08-05-091530-b.sql",
            "-- migrations:no-transaction\n"
            "CREATE INDEX CONCURRENTLY a_id_idx ON a (id);\n"
            "CREATE INDEX CONCURRENTLY a_id_idx2 ON a (id);\n",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("more than one statement" in line for line in lines)

        # The scan runs before anything executes, so even the earlier, valid file is untouched.
        assert not await table_exists(conn, "a")
        assert await Tracker(conn).applied_rows() == []

    async def test_a_multi_statement_no_transaction_file_is_refused_in_dry_run_too(
        self, conn, migrations_dir, printed
    ):
        lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "-- migrations:no-transaction\nCREATE TABLE a (id int);\nCREATE TABLE b (id int);\n",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", True, None, "test", out) == 1
        assert any("more than one statement" in line for line in lines)

    async def test_a_multi_statement_file_without_the_directive_is_still_fine(
        self, conn, migrations_dir, printed
    ):
        _lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "CREATE TABLE a (id int);\nCREATE TABLE b (id int);",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0

    async def test_a_failed_no_transaction_file_does_not_claim_a_clean_stop(
        self, conn, migrations_dir, printed
    ):
        """Nothing rolls back outside a transaction: a failed CREATE UNIQUE INDEX
        CONCURRENTLY leaves an invalid index behind and no tracking row, so a re-run dies on
        "relation already exists". The message must say so, not "nothing was applied"."""

        lines, out = printed
        write(
            migrations_dir,
            "2026-08-04-091530-a.sql",
            "CREATE TABLE a (id int);\nINSERT INTO a VALUES (1), (1);",
        )
        write(
            migrations_dir,
            "2026-08-05-091530-b.sql",
            "-- migrations:no-transaction\nCREATE UNIQUE INDEX CONCURRENTLY a_id_uidx ON a (id);",
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("OUTSIDE a transaction" in line for line in lines)
        assert any("PARTIALLY applied" in line for line in lines)
        assert not any("Nothing after this migration was applied" in line for line in lines)

        # Not just wording: the leftover invalid index really is there.
        async with conn.cursor() as cur:
            await cur.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('a_id_uidx')")
            assert (await cur.fetchone())[0] is False

    async def test_a_failed_transactional_file_keeps_the_clean_stop_wording(
        self, conn, migrations_dir, printed
    ):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "THIS IS NOT SQL;")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("Nothing after this migration was applied" in line for line in lines)
        assert not any("OUTSIDE a transaction" in line for line in lines)

    async def test_missing_advice_does_not_point_at_repair(self, conn, migrations_dir, printed):
        """`repair` re-reads the file to re-stamp its checksum, so for MISSING it can only
        answer "no such migration file". The shared footer used to send operators there
        anyway, with the one working remedy documented nowhere."""

        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.unlink()
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")
        lines.clear()

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("DELETE FROM migrations WHERE name = '2026-08-04-091530-a.sql';" in line for line in lines)
        assert any("cannot help" in line for line in lines)
        assert not any("run `migrations repair" in line for line in lines)

        # And the command the old footer pointed at really does refuse.
        assert await cmd_repair(conn, migrations_dir, "migrations", "2026-08-04-091530-a.sql", out) == 1

    async def test_drift_advice_still_points_at_repair(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- edited")
        write(migrations_dir, "2026-08-05-091530-b.sql", "CREATE TABLE b (id int);")
        lines.clear()

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("run `migrations repair" in line for line in lines)
        assert not any("DELETE FROM" in line for line in lines)

    async def test_reports_a_hijacked_tracking_table_instead_of_crashing(self, conn, migrations_dir, printed):
        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE migrations (id serial primary key, version int)")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert any("is not a migration tracking table" in line for line in lines)

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

    async def test_a_file_with_no_tracking_row_is_an_error(self, conn, migrations_dir, printed):
        """The file exists on disk but was never applied, so there is no checksum to re-stamp."""

        lines, out = printed
        write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_repair(conn, migrations_dir, "migrations", "2026-08-04-091530-a.sql", out) == 1
        assert any("no tracking row" in line for line in lines)

    async def test_a_name_with_no_file_at_all_is_an_error(self, conn, migrations_dir, printed):
        lines, out = printed

        assert await cmd_repair(conn, migrations_dir, "migrations", "2026-08-04-091530-nope.sql", out) == 1
        assert any("no such migration file" in line for line in lines)

    async def test_a_path_outside_the_migrations_directory_is_refused(
        self, conn, migrations_dir, printed, tmp_path
    ):
        """A traversing `name` used to stamp the *outside* file's checksum against the real
        migration's name, reporting a successful repair while leaving it stuck in DRIFT."""

        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- deliberate edit")

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "2026-08-04-091530-a.sql").write_text("SELECT 'something else entirely';")
        lines.clear()

        code = await cmd_repair(
            conn, migrations_dir, "migrations", "../elsewhere/2026-08-04-091530-a.sql", out
        )

        assert code == 1
        assert any("plain migration filename" in line for line in lines)

        # The real migration is still in DRIFT, not falsely stamped with the outside checksum.
        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 1
        assert any("DRIFT" in line for line in lines)

    async def test_an_absolute_path_is_refused(self, conn, migrations_dir, printed):
        lines, out = printed
        path = write(migrations_dir, "2026-08-04-091530-a.sql", "CREATE TABLE a (id int);")

        assert await cmd_repair(conn, migrations_dir, "migrations", str(path), out) == 1
        assert any("plain migration filename" in line for line in lines)


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
        result = connect_kwargs(
            {"hostname": "db", "username": "app", "password": "s", "database": "appdb"}
        )

        assert result["port"] == 5432
        assert result["sslmode"] == "prefer"

    def test_resolve_targets_default_when_config_has_no_migrations_key(self, tmp_path):
        targets = resolve_targets({"current_path": str(tmp_path)})

        assert list(targets) == ["main"]
        assert targets["main"] == Target(
            name="main", db="main", directory=tmp_path / "data" / "migrations", table="migrations"
        )

    def test_resolve_targets_default_when_migrations_key_is_empty(self, tmp_path):
        targets = resolve_targets({"current_path": str(tmp_path), "migrations": {}})

        assert list(targets) == ["main"]
        assert targets["main"] == Target(
            name="main", db="main", directory=tmp_path / "data" / "migrations", table="migrations"
        )

    def test_resolve_targets_honour_flat_explicit_values(self, tmp_path):
        targets = resolve_targets(
            {"current_path": str(tmp_path), "migrations": {"dir": "sql/steps", "table": "schema_history"}}
        )

        assert list(targets) == ["main"]
        assert targets["main"] == Target(
            name="main", db="main", directory=tmp_path / "sql" / "steps", table="schema_history"
        )

    def test_resolve_targets_flat_shape_accepts_an_absolute_dir(self, tmp_path):
        targets = resolve_targets({"current_path": "/somewhere/else", "migrations": {"dir": str(tmp_path)}})

        assert targets["main"].directory == tmp_path

    def test_resolve_targets_new_shape_preserves_declaration_order(self, tmp_path):
        targets = resolve_targets(
            {
                "current_path": str(tmp_path),
                "db": {"main": {}, "gis": {}},
                "migrations": {
                    "targets": {
                        "main": {"db": "main", "dir": "data/migrations", "table": "migrations"},
                        "gis": {"db": "gis", "dir": "data/migrations_gis", "table": "gis_migrations"},
                    }
                },
            }
        )

        assert list(targets) == ["main", "gis"]
        assert targets["main"] == Target(
            name="main", db="main", directory=tmp_path / "data" / "migrations", table="migrations"
        )
        assert targets["gis"] == Target(
            name="gis", db="gis", directory=tmp_path / "data" / "migrations_gis", table="gis_migrations"
        )

    def test_resolve_targets_per_target_defaults(self, tmp_path):
        targets = resolve_targets(
            {"current_path": str(tmp_path), "db": {"gis": {}}, "migrations": {"targets": {"gis": {}}}}
        )

        assert targets["gis"] == Target(
            name="gis", db="gis", directory=tmp_path / "data" / "migrations", table="migrations"
        )

    def test_resolve_targets_new_shape_relative_dir_resolves_against_current_path(self, tmp_path):
        targets = resolve_targets(
            {
                "current_path": str(tmp_path),
                "db": {"gis": {}},
                "migrations": {"targets": {"gis": {"dir": "sql/gis"}}},
            }
        )

        assert targets["gis"].directory == tmp_path / "sql" / "gis"

    def test_resolve_targets_new_shape_absolute_dir_passes_through(self, tmp_path):
        targets = resolve_targets(
            {
                "current_path": "/somewhere/else",
                "db": {"gis": {}},
                "migrations": {"targets": {"gis": {"dir": str(tmp_path)}}},
            }
        )

        assert targets["gis"].directory == tmp_path

    def test_resolve_targets_unknown_db_raises(self, tmp_path):
        with pytest.raises(MigrationError, match="gis.*db"):
            resolve_targets(
                {
                    "current_path": str(tmp_path),
                    "db": {"main": {}},
                    "migrations": {"targets": {"gis": {"db": "gis"}}},
                }
            )

    def test_resolve_targets_empty_targets_raises(self, tmp_path):
        with pytest.raises(MigrationError, match="targets"):
            resolve_targets({"current_path": str(tmp_path), "db": {"main": {}}, "migrations": {"targets": {}}})

    def test_resolve_targets_flat_keys_alongside_targets_raises(self, tmp_path):
        with pytest.raises(MigrationError, match="dir"):
            resolve_targets(
                {
                    "current_path": str(tmp_path),
                    "db": {"main": {}},
                    "migrations": {"dir": "sql/steps", "targets": {"main": {}}},
                }
            )
