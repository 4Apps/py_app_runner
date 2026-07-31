"""Covers what brief Step 6's shell harness was meant to prove: the `migrations`
subparser registers on a real ArgumentParser without colliding with the top-level
`--dry-run`, and `init_service` actually dispatches to the commands against a real
database when driven the way `runner.py` drives it."""

import argparse
import logging
import socket
from argparse import Namespace

import pytest

from py_app_runner.migrations._service import init_service
from py_app_runner.migrations._service_args import reg_subparsers
from py_app_runner.registry import AppRegistry
from tests.migrations_pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, pg_dsn_for

psycopg = pytest.importorskip("psycopg")


def build_parser() -> argparse.ArgumentParser:
    # Mirrors the pieces of runner.py's top-level parser this test cares about: a
    # `--dry-run` flag defined before subparsers exist, then `migrations` registered
    # the same way `runner.py` registers every service's subparser.
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="service")
    reg_subparsers(subparsers, _pybridge=None, _base_logger=logging.getLogger("test"))
    return parser


class TestRegSubparsers:
    def test_all_five_subcommands_set_step_to_their_own_name(self):
        parser = build_parser()

        for argv, expected_step in (
            (["migrations", "status"], "status"),
            (["migrations", "apply"], "apply"),
            (["migrations", "baseline"], "baseline"),
            (["migrations", "new", "widgets"], "new"),
            (["migrations", "repair", "2026-08-04-091530-a.sql"], "repair"),
        ):
            args = parser.parse_args(argv)
            assert args.step == expected_step

    def test_status_check_flag(self):
        parser = build_parser()
        assert parser.parse_args(["migrations", "status"]).check is False
        assert parser.parse_args(["migrations", "status", "--check"]).check is True

    def test_apply_local_dry_run_does_not_collide_with_the_top_level_flag(self):
        parser = build_parser()

        # The subparser's own --dry-run, given after the subcommand.
        args = parser.parse_args(["migrations", "apply", "--dry-run"])
        assert args.dry_run is True

        # No flag at all: subparser default wins, false.
        args = parser.parse_args(["migrations", "apply"])
        assert args.dry_run is False

        # The top-level flag still works for a command with no --dry-run of its own.
        args = parser.parse_args(["--dry-run", "migrations", "status"])
        assert args.dry_run is True

    def test_apply_to_flag(self):
        parser = build_parser()
        args = parser.parse_args(["migrations", "apply", "--to", "2026-08-04-091530"])
        assert args.to == "2026-08-04-091530"

    def test_baseline_flags(self):
        parser = build_parser()
        args = parser.parse_args(["migrations", "baseline", "--to", "2026-08-04-091530", "--yes"])
        assert args.to == "2026-08-04-091530"
        assert args.yes is True

    def test_new_requires_a_name(self):
        parser = build_parser()
        args = parser.parse_args(["migrations", "new", "add widgets table"])
        assert args.name == "add widgets table"

    def test_repair_requires_a_name(self):
        parser = build_parser()
        args = parser.parse_args(["migrations", "repair", "2026-08-04-091530-a.sql"])
        assert args.name == "2026-08-04-091530-a.sql"


@pytest.fixture
def saved_registry():
    # AppRegistry is a process-wide singleton; init_service reads it, so this test must
    # not leak its throwaway config into whatever runs after it.
    original = AppRegistry.config()
    yield
    AppRegistry.configure(config=original, users_model=object, api_keys_model=object)


class TestInitServiceEndToEnd:
    async def test_status_apply_status_round_trip_against_a_real_database(self, tmp_path, saved_registry):
        migrations_dir = tmp_path / "data" / "migrations"
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "2026-08-04-091530-cli-smoke.sql").write_text("CREATE TABLE cli_smoke (id int);")

        db_name = "par_test_service_cli"
        async with pg_dsn_for(db_name):
            config = {
                "current_path": str(tmp_path),
                "app_version": "test-version",
                "db": {
                    "main": {
                        "hostname": PG_HOST,
                        "port": PG_PORT,
                        "username": PG_USER,
                        "password": PG_PASSWORD,
                        "database": db_name,
                    }
                },
            }
            AppRegistry.configure(config=config, users_model=object, api_keys_model=object)
            logger = logging.getLogger("test")

            with pytest.raises(SystemExit) as excinfo:
                await init_service(Namespace(step="status", check=True), None, logger)
            assert excinfo.value.code == 1  # nothing applied yet

            with pytest.raises(SystemExit) as excinfo:
                await init_service(Namespace(step="apply", dry_run=False, to=None), None, logger)
            assert excinfo.value.code == 0

            with pytest.raises(SystemExit) as excinfo:
                await init_service(Namespace(step="status", check=True), None, logger)
            assert excinfo.value.code == 0  # everything applied now

            admin_dsn = f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={db_name}"
            async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as check_conn:
                async with check_conn.cursor() as cur:
                    await cur.execute("SELECT applied_by FROM migrations")
                    row = await cur.fetchone()
                    assert row is not None
                    assert row[0] == f"test-version @ {socket.gethostname()}"

    async def test_new_exits_zero_and_writes_the_file_without_touching_the_database(self, tmp_path, saved_registry):
        migrations_dir = tmp_path / "data" / "migrations"
        migrations_dir.mkdir(parents=True)

        config = {"current_path": str(tmp_path)}
        AppRegistry.configure(config=config, users_model=object, api_keys_model=object)

        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="new", name="add widgets table"), None, logging.getLogger("test"))

        assert excinfo.value.code == 0
        created = list(migrations_dir.glob("*-add-widgets-table.sql"))
        assert len(created) == 1
