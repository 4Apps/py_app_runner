import logging
import pathlib
import socket
from argparse import Namespace
from datetime import UTC, datetime
from typing import Any

import psycopg

from py_app_runner.migrations.commands import (
    cmd_apply,
    cmd_baseline,
    cmd_new,
    cmd_repair,
    cmd_status,
)
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry

_DEFAULT_DIR = "data/migrations"
_DEFAULT_TABLE = "migrations"


def connect_kwargs(db_config: dict[str, Any]) -> dict[str, Any]:
    """PgsqlConfig uses hostname/username/database; psycopg wants host/user/dbname.

    autocommit is on because this service manages transactions per migration file
    explicitly - an implicit outer transaction would swallow the per-file boundaries and
    make the no-transaction directive impossible to honour.
    """

    return {
        "host": db_config["hostname"],
        "port": db_config.get("port") or 5432,
        "user": db_config["username"],
        "password": db_config["password"],
        "dbname": db_config["database"],
        "sslmode": db_config.get("ssl") or "prefer",
        "autocommit": True,
    }


def migrations_settings(config: dict[str, Any]) -> tuple[pathlib.Path, str]:
    settings = config.get("migrations") or {}
    directory = pathlib.Path(settings.get("dir") or _DEFAULT_DIR)
    if not directory.is_absolute():
        directory = pathlib.Path(config["current_path"]) / directory

    return directory, settings.get("table") or _DEFAULT_TABLE


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    config = AppRegistry.config()
    directory, table = migrations_settings(config)
    out = print

    if args.step == "new":
        raise SystemExit(cmd_new(directory, args.name, datetime.now(UTC), out))

    applied_by = f"{config.get('app_version', 'unknown')} @ {socket.gethostname()}"
    logger.debug(f"migrations: dir={directory} table={table}")

    # runner.py catches Exception around init_service, logs it and returns normally - which
    # exits 0. For a long-running service that is deliberate, but here it would tell an
    # unattended playbook that migrations succeeded when the database was merely unreachable,
    # and the playbook would go on to restart services against an unmigrated schema. Silent
    # success is the one outcome this tool must never produce, so every non-SystemExit failure
    # is converted into a non-zero exit here, inside the service, without touching runner.py.
    # SystemExit derives from BaseException, so the happy-path exit below is not re-wrapped.
    code = 1
    try:
        async with await psycopg.AsyncConnection.connect(**connect_kwargs(config["db"]["main"])) as conn:
            if args.step == "status":
                code = await cmd_status(conn, directory, table, args.check, out)
            elif args.step == "apply":
                dry_run = getattr(args, "dry_run", False)
                code = await cmd_apply(conn, directory, table, dry_run, args.to, applied_by, out)
            elif args.step == "baseline":
                code = await cmd_baseline(conn, directory, table, args.to, args.yes, applied_by, input, out)
            elif args.step == "repair":
                code = await cmd_repair(conn, directory, table, args.name, out)
            else:
                out(f"error: unknown migrations command {args.step!r}")
                code = 1

    except Exception:
        logger.exception("migrations: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)
