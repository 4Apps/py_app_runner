"""The migration commands on a SQLite database. The Postgres suite covers the command logic
itself; this covers what differs: statement splitting, the transaction around each file,
the tracker and the file lock."""

import asyncio
import pathlib

import pytest
import pytest_asyncio

from py_app_runner import file_lock
from py_app_runner.migrations._service import connect, connect_kwargs, resolve_targets
from py_app_runner.migrations.commands import cmd_apply, cmd_baseline, cmd_repair, cmd_status
from py_app_runner.migrations.discovery import MigrationError, split_statements


@pytest.fixture
def db_config(tmp_path) -> dict[str, str]:
    return {"driver": "sqlite", "database": str(tmp_path / "app.sqlite3")}


@pytest_asyncio.fixture
async def conn(db_config):
    async with connect(db_config) as connection:
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


async def tables(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in await cur.fetchall()}


async def tracked(conn) -> list[str]:
    cur = await conn.execute("SELECT name FROM migrations ORDER BY name")
    return [row["name"] for row in await cur.fetchall()]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("CREATE TABLE a (x int); CREATE TABLE b (x int);", ["CREATE TABLE a (x int);", "CREATE TABLE b (x int);"]),
        ("INSERT INTO t VALUES ('a;b');", ["INSERT INTO t VALUES ('a;b');"]),
        (
            "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x = 1; UPDATE t SET y = 2; END;",
            ["CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x = 1; UPDATE t SET y = 2; END;"],
        ),
        ("SELECT 1;\n-- trailing comment\n", ["SELECT 1;"]),
        ("-- only a comment;\n", []),
        ("SELECT 1", ["SELECT 1;"]),
    ],
)
def test_split_statements(script: str, expected: list[str]):
    assert split_statements(script) == expected


class TestApply:
    async def test_applies_multi_statement_files_in_order_and_records_them(self, conn, migrations_dir, printed):
        lines, out = printed
        (migrations_dir / "2026-08-04-091530-a.sql").write_text(
            "CREATE TABLE a (id INTEGER PRIMARY KEY, note TEXT);\nINSERT INTO a (note) VALUES ('x;y');\n-- done\n"
        )
        (migrations_dir / "2026-08-04-091531-b.sql").write_text("CREATE TABLE b (a_id INTEGER REFERENCES a (id))")

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert {"a", "b"} <= await tables(conn)
        assert await tracked(conn) == ["2026-08-04-091530-a.sql", "2026-08-04-091531-b.sql"]

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 0
        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 0
        assert lines[-1] == "Database is up to date; nothing to apply."

    async def test_a_failing_file_rolls_back_whole_and_records_nothing(self, conn, migrations_dir, printed):
        lines, out = printed
        (migrations_dir / "2026-08-04-091530-a.sql").write_text(
            "CREATE TABLE a (id int);\nINSERT INTO nope VALUES (1);"
        )

        assert await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out) == 1
        assert "a" not in await tables(conn)
        assert await tracked(conn) == []
        assert any("FAILED" in line for line in lines)

    async def test_waits_for_another_apply_holding_the_lock(self, conn, db_config, migrations_dir, printed):
        _lines, out = printed
        (migrations_dir / "2026-08-04-091530-a.sql").write_text("CREATE TABLE a (id int);")

        fd = file_lock.lock(f"{db_config['database']}.migrations.lock")
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.5):
                    await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        finally:
            file_lock.unlock(fd)

        assert "a" not in await tables(conn)


class TestTracker:
    async def test_drift_then_repair(self, conn, migrations_dir, printed):
        lines, out = printed
        path = migrations_dir / "2026-08-04-091530-a.sql"
        path.write_text("CREATE TABLE a (id int);")
        await cmd_apply(conn, migrations_dir, "migrations", False, None, "test", out)
        path.write_text("CREATE TABLE a (id int); -- edited")

        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 1
        assert any("DRIFT" in line for line in lines)

        assert await cmd_repair(conn, migrations_dir, "migrations", path.name, out) == 0
        assert await cmd_status(conn, migrations_dir, "migrations", check=True, out=out) == 0

    async def test_baseline_stamps_without_executing(self, conn, migrations_dir, printed):
        _lines, out = printed
        (migrations_dir / "2026-08-04-091530-a.sql").write_text("CREATE TABLE a (id int);")

        assert await cmd_baseline(conn, migrations_dir, "migrations", None, True, "test", input, out) == 0
        assert await tracked(conn) == ["2026-08-04-091530-a.sql"]
        assert "a" not in await tables(conn)

    async def test_refuses_an_unrelated_table_of_the_same_name(self, conn, migrations_dir, printed):
        lines, out = printed
        await conn.execute("CREATE TABLE migrations (id INTEGER PRIMARY KEY, title TEXT)")

        assert await cmd_status(conn, migrations_dir, "migrations", check=False, out=out) == 1
        assert any("not a migration tracking table" in line for line in lines)


class TestConfig:
    def test_two_targets_on_one_sqlite_file_cannot_share_a_tracking_table(self, tmp_path, db_config):
        config = {
            "current_path": str(tmp_path),
            "db": {"main": db_config, "other": {"driver": "sqlite", "database": f"{tmp_path}/./app.sqlite3"}},
            "migrations": {"targets": {"main": {}, "other": {}}},
        }

        with pytest.raises(MigrationError, match="same physical database"):
            resolve_targets(config)

    def test_postgres_only_commands_refuse_a_sqlite_database(self, db_config):
        with pytest.raises(ValueError, match="needs Postgres"):
            connect_kwargs(db_config)
