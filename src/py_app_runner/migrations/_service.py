import logging
import pathlib
import socket
from argparse import Namespace
from dataclasses import dataclass
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
from py_app_runner.migrations.discovery import MigrationError
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry

_DEFAULT_DIR = "data/migrations"
_DEFAULT_TABLE = "migrations"
_DEFAULT_TARGET_NAME = "main"


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


@dataclass(frozen=True)
class Target:
    name: str
    db: str
    directory: pathlib.Path
    table: str


def _resolve_directory(raw_dir: str | None, current_path: Any) -> pathlib.Path:
    directory = pathlib.Path(raw_dir or _DEFAULT_DIR)
    if not directory.is_absolute():
        directory = pathlib.Path(current_path) / directory

    return directory


def resolve_targets(config: dict[str, Any]) -> dict[str, Target]:
    """Resolve `config["migrations"]` into an ordered mapping of target name -> Target.

    Two shapes are supported and never mixed. The flat shape (today's shape, and what all
    four consuming projects still ship) synthesises a single target named "main" against
    db "main" - this is the backward-compatibility path and must keep working with zero
    config changes. The "targets" shape opts a project into multiple named targets, each
    with its own db/dir/table; presence of the "targets" key alone selects it.
    """

    settings = config.get("migrations") or {}

    if "targets" in settings:
        flat_keys = {"dir", "table"} & settings.keys()
        if flat_keys:
            raise MigrationError(
                f'config["migrations"] mixes "targets" with the flat key(s) '
                f"{', '.join(sorted(flat_keys))}; that is ambiguous, not merged. Move dir/table "
                f'into each entry under config["migrations"]["targets"] instead.'
            )

        raw_targets = settings["targets"]
        if not raw_targets:
            raise MigrationError(
                'config["migrations"]["targets"] is present but empty; declare at least one target.'
            )

        db_config = config.get("db") or {}
        current_path = config["current_path"]
        targets: dict[str, Target] = {}
        for name, raw_entry in raw_targets.items():
            entry = raw_entry or {}
            db_name = entry.get("db") or name
            if db_name not in db_config:
                raise MigrationError(
                    f"migrations target {name!r} names db {db_name!r}, which is not configured under "
                    f'config["db"]; configured db keys are: {", ".join(sorted(db_config)) or "none"}.'
                )
            targets[name] = Target(
                name=name,
                db=db_name,
                directory=_resolve_directory(entry.get("dir"), current_path),
                table=entry.get("table") or _DEFAULT_TABLE,
            )

        return targets

    directory = _resolve_directory(settings.get("dir"), config["current_path"])
    table = settings.get("table") or _DEFAULT_TABLE
    target = Target(name=_DEFAULT_TARGET_NAME, db=_DEFAULT_TARGET_NAME, directory=directory, table=table)
    return {_DEFAULT_TARGET_NAME: target}


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    config = AppRegistry.config()
    target = next(iter(resolve_targets(config).values()))
    directory, table = target.directory, target.table
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
        async with await psycopg.AsyncConnection.connect(**connect_kwargs(config["db"][target.db])) as conn:
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
