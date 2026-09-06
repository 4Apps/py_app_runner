import argparse
import logging
from argparse import Namespace

import psycopg
import pytest

from py_app_runner.cron._service import app_argv, init_service
from py_app_runner.cron._service_args import reg_subparsers
from py_app_runner.cron.execute import PgLock
from py_app_runner.registry import AppRegistry
from tests.migrations_pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, pg_dsn_for


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="service")
    reg_subparsers(subparsers, _pybridge=None, _base_logger=logging.getLogger("test"))
    return parser


class TestRegSubparsers:
    def test_subcommands_and_flags(self):
        parser = build_parser()

        assert parser.parse_args(["cron", "list"]).step == "list"
        assert parser.parse_args(["cron", "work"]).step == "work"

        args = parser.parse_args(["cron", "run", "--job", "sync"])
        assert args.step == "run"
        assert args.job == "sync"
        assert args.dry_run is False

    def test_dry_run_survives_either_position(self):
        parser = build_parser()

        assert parser.parse_args(["--dry-run", "cron", "run"]).dry_run is True
        assert parser.parse_args(["cron", "run", "--dry-run"]).dry_run is True


class TestAppArgv:
    def test_child_gets_the_scheduler_log_level(self):
        assert app_argv(Namespace(v="debug"))[2:] == ["-v", "debug"]
        assert app_argv(Namespace())[2:] == ["-v", "info"]


@pytest.fixture
def saved_registry():
    original = AppRegistry.config()
    yield
    AppRegistry.configure(config=original, users_model=object, api_keys_model=object)


class TestPgLock:
    async def test_second_session_cannot_take_a_held_lock(self):
        async with pg_dsn_for("par_test_cron_lock") as dsn:
            async with (
                await psycopg.AsyncConnection.connect(dsn, autocommit=True) as a,
                await psycopg.AsyncConnection.connect(dsn, autocommit=True) as b,
            ):
                first, second = PgLock(a), PgLock(b)

                assert await first.acquire("sync")
                assert not await second.acquire("sync")
                assert await second.acquire("other")

                await first.release("sync")
                assert await second.acquire("sync")


class TestInitService:
    async def test_config_error_exits_2_and_list_exits_0(self, saved_registry, capsys):
        AppRegistry.configure(
            config={"cron": {"jobs": {"x": {"schedule": "bad", "command": "y"}}}},
            users_model=object,
            api_keys_model=object,
        )
        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="list"), None, logging.getLogger("test"))
        assert excinfo.value.code == 2
        assert "Job 'x'" in capsys.readouterr().out

        AppRegistry.configure(
            config={"cron": {"jobs": {"x": {"schedule": "0 4 * * *", "command": "ping"}}}},
            users_model=object,
            api_keys_model=object,
        )
        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="list"), None, logging.getLogger("test"))
        assert excinfo.value.code == 0
        assert "0 4 * * *" in capsys.readouterr().out

    async def test_unknown_job_exits_2_and_dry_run_needs_no_database(self, saved_registry, capsys):
        AppRegistry.configure(
            config={"cron": {"jobs": {"x": {"schedule": "0 4 * * *", "command": "ping"}}}},
            users_model=object,
            api_keys_model=object,
        )
        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="run", job="nope"), None, logging.getLogger("test"))
        assert excinfo.value.code == 2

        with pytest.raises(SystemExit) as excinfo:
            await init_service(Namespace(step="run", job="x", dry_run=True), None, logging.getLogger("test"))
        assert excinfo.value.code == 0
        assert "would run x: ping" in capsys.readouterr().out

    async def test_run_takes_the_lock_on_the_configured_database(self, saved_registry):
        db_name = "par_test_cron_service"
        async with pg_dsn_for(db_name) as dsn:
            AppRegistry.configure(
                config={
                    "db": {
                        "main": {
                            "hostname": PG_HOST,
                            "port": PG_PORT,
                            "username": PG_USER,
                            "password": PG_PASSWORD,
                            "database": db_name,
                        }
                    },
                    "cron": {"jobs": {"x": {"schedule": "0 4 * * *", "command": "--version"}}},
                },
                users_model=object,
                api_keys_model=object,
            )

            # The child is `python3 <this argv[0]> -v info --version`; pytest's argv[0] is
            # not app.py, so the exit code is whatever that does - only that a run happened
            # and the lock was held and released is asserted.
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as other:
                assert await PgLock(other).acquire("x")
                with pytest.raises(SystemExit) as excinfo:
                    await init_service(Namespace(step="run", job="x"), None, logging.getLogger("test"))
                assert excinfo.value.code == 0  # skipped, not failed
                await PgLock(other).release("x")

                with pytest.raises(SystemExit):
                    await init_service(Namespace(step="run", job="x"), None, logging.getLogger("test"))
                assert await PgLock(other).acquire("x")
