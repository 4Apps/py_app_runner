"""Covers what brief Step 6's shell harness was meant to prove: the `migrations`
subparser registers on a real ArgumentParser without colliding with the top-level
`--dry-run`, and `init_service` actually dispatches to the commands against a real
database when driven the way `runner.py` drives it."""

import argparse
import logging
import os
import pathlib
import socket
import subprocess
import sys
from argparse import Namespace

import psycopg
import pytest

from py_app_runner.migrations._service import init_service
from py_app_runner.migrations._service_args import reg_subparsers
from py_app_runner.registry import AppRegistry
from tests.migrations_pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, pg_dsn_for


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

        # The dangerous case: --dry-run given BEFORE the subcommand, with `apply` not
        # repeating it. Without `default=SUPPRESS` on the subparser's own --dry-run,
        # _SubParsersAction copies apply's unset default (False) back onto the parent
        # namespace and silently turns this into a real, non-dry-run apply.
        args = parser.parse_args(["--dry-run", "migrations", "apply"])
        assert args.dry_run is True

        # Both given: still true.
        args = parser.parse_args(["--dry-run", "migrations", "apply", "--dry-run"])
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


_RUNNER_APP = """
import os

from py_app_runner.registry import AppRegistry
from py_app_runner.runner import main

AppRegistry.configure(
    config={{
        "environment": "test",
        "current_path": os.getcwd(),
        "app_version": "exit-code-probe",
        "services": ["migrations"],
        "db": {{
            "main": {{
                "hostname": {hostname!r},
                "port": {port!r},
                "username": {username!r},
                "password": {password!r},
                "database": {database!r},
            }}
        }},
    }},
    users_model=object,
    api_keys_model=object,
)

main()
"""


def build_runner_app(root: pathlib.Path, hostname: str, database: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "data" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("APP_ENV=test\n")
    (root / "src" / "app.py").write_text(
        _RUNNER_APP.format(
            hostname=hostname,
            port=PG_PORT,
            username=PG_USER,
            password=PG_PASSWORD,
            database=database,
        )
    )


def run_app(root: pathlib.Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)

    return subprocess.run(
        [sys.executable, "src/app.py", *argv],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestProcessExitCodes:
    """The missing layer. Every other command test calls `cmd_*` directly and checks the
    returned int; nothing checked what the *process* exits with. `runner.py` catches
    `Exception` around `init_service`, logs it and returns normally - so before this,
    `migrations apply` against an unreachable database logged a traceback and exited 0, and
    an unattended playbook happily carried on against an unmigrated schema."""

    async def test_a_database_failure_exits_one_rather_than_zero(self, tmp_path, saved_registry):
        migrations_dir = tmp_path / "data" / "migrations"
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "2026-08-04-091530-never-runs.sql").write_text("CREATE TABLE never_runs (id int);")

        config = {
            "current_path": str(tmp_path),
            "db": {
                "main": {
                    "hostname": "no-such-host-at-all.invalid",
                    "port": PG_PORT,
                    "username": PG_USER,
                    "password": PG_PASSWORD,
                    "database": "nope",
                }
            },
        }
        AppRegistry.configure(config=config, users_model=object, api_keys_model=object)

        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="apply", dry_run=False, to=None), None, logging.getLogger("test"))

        assert excinfo.value.code == 1

    async def test_a_failure_mid_run_exits_one(self, tmp_path, saved_registry, monkeypatch):
        """Not just the connect: anything raising a plain Exception once connected - a
        permissions error, a dropped connection - must reach the same non-zero exit."""

        migrations_dir = tmp_path / "data" / "migrations"
        migrations_dir.mkdir(parents=True)

        db_name = "par_test_service_midrun"
        async with pg_dsn_for(db_name):
            config = {
                "current_path": str(tmp_path),
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

            async def boom(*_args, **_kwargs):
                raise RuntimeError("connection dropped mid-run")

            monkeypatch.setattr("py_app_runner.migrations._service.cmd_apply", boom)

            with pytest.raises(SystemExit) as excinfo:
                await init_service(Namespace(step="apply", dry_run=False, to=None), None, logging.getLogger("test"))

            assert excinfo.value.code == 1

    async def test_the_happy_path_exit_code_is_not_re_wrapped(self, tmp_path, saved_registry):
        """`SystemExit` derives from `BaseException`, so the new `except Exception` must not
        catch the normal exit and turn a successful apply into a failure."""

        migrations_dir = tmp_path / "data" / "migrations"
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "2026-08-04-091530-ok.sql").write_text("CREATE TABLE ok (id int);")

        db_name = "par_test_service_happy"
        async with pg_dsn_for(db_name):
            config = {
                "current_path": str(tmp_path),
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

            with pytest.raises(SystemExit) as excinfo:
                await init_service(Namespace(step="apply", dry_run=False, to=None), None, logging.getLogger("test"))

            assert excinfo.value.code == 0

    def test_the_real_runner_main_propagates_the_exit_code(self, tmp_path):
        """Driven through `runner.py`'s own `main()` in a real child process, because an
        exit-code claim is not verifiable by reading `init_service` alone."""

        failing = tmp_path / "failing"
        build_runner_app(failing, "no-such-host-at-all.invalid", "nope")
        (failing / "data" / "migrations" / "2026-08-04-091530-never-runs.sql").write_text("SELECT 1;")

        result = run_app(failing, "migrations", "apply")
        assert result.returncode == 1, result.stderr

    async def test_the_real_runner_main_exits_zero_on_success(self, tmp_path):
        db_name = "par_test_runner_main"
        async with pg_dsn_for(db_name):
            working = tmp_path / "working"
            build_runner_app(working, PG_HOST, db_name)
            (working / "data" / "migrations" / "2026-08-04-091530-ok.sql").write_text("CREATE TABLE ok (id int);")

            result = run_app(working, "migrations", "apply")
            assert result.returncode == 0, result.stderr
