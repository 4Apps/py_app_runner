import logging
import pathlib
import socket
from argparse import Namespace
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from py_app_runner.migrations.commands import (
    Out,
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


def _resolve_directory(raw_dir: str | None, config: dict[str, Any]) -> pathlib.Path:
    directory = pathlib.Path(raw_dir or _DEFAULT_DIR)
    if directory.is_absolute():
        return directory

    current_path = config.get("current_path")
    if current_path is None:
        raise MigrationError(
            f'migrations dir {str(directory)!r} is relative but config has no "current_path" to '
            f"resolve it against; either set current_path or use an absolute dir."
        )

    return pathlib.Path(current_path) / directory


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
                directory=_resolve_directory(entry.get("dir"), config),
                table=entry.get("table") or _DEFAULT_TABLE,
            )

        return targets

    directory = _resolve_directory(settings.get("dir"), config)
    table = settings.get("table") or _DEFAULT_TABLE
    target = Target(name=_DEFAULT_TARGET_NAME, db=_DEFAULT_TARGET_NAME, directory=directory, table=table)
    return {_DEFAULT_TARGET_NAME: target}


# status and apply run every configured target unless narrowed with --target: status is a
# read-only report, and apply's own per-target failure handling (see below) is what keeps a
# fan-out safe. new/baseline/repair are single-item: baseline in particular writes tracking
# rows without ever running the SQL, so a mis-stamped baseline against the wrong target
# produces a permanently green `status` over a database that never got its tables - the
# worst failure this tool can produce, silently. Guessing which target that should be is not
# acceptable, so those three refuse outright when more than one target is configured and
# --target was not given.
_FAN_OUT_STEPS = frozenset({"status", "apply"})


def _out_for(name: str, multi: bool) -> Out:
    """Single-target output must stay byte-identical to today, so `print` is used directly
    below whenever only one target is in play. In the multi-target case, each line is
    prefixed with its target's name - except blank lines, which commands use as visual
    separators; a `[name] ` prefix on those would just be noise."""

    if not multi:
        return print

    def prefixed(line: str) -> None:
        print(f"[{name}] {line}" if line else "")

    return prefixed


def _select_targets(step: str, requested: str | None, targets: dict[str, Target]) -> list[Target]:
    if requested is not None:
        if requested not in targets:
            print(
                f"error: no migrations target {requested!r}; configured targets are: "
                f"{', '.join(targets) or 'none'}."
            )
            raise SystemExit(1)

        return [targets[requested]]

    if step in _FAN_OUT_STEPS or len(targets) == 1:
        return list(targets.values())

    print(
        f"error: migrations {step!r} needs --target since more than one target is configured; "
        f"configured targets are: {', '.join(targets)}."
    )
    raise SystemExit(1)


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    config = AppRegistry.config()
    targets = resolve_targets(config)
    selected = _select_targets(args.step, getattr(args, "target", None), targets)
    multi = len(selected) > 1

    if args.step == "new":
        # Synchronous and needs no database - dispatched before any connection is opened.
        # _select_targets already refused ambiguity above, so exactly one target here.
        target = selected[0]
        raise SystemExit(cmd_new(target.directory, args.name, datetime.now(UTC), _out_for(target.name, multi)))

    applied_by = f"{config.get('app_version', 'unknown')} @ {socket.gethostname()}"

    # runner.py catches Exception around init_service, logs it and returns normally - which
    # exits 0. For a long-running service that is deliberate, but here it would tell an
    # unattended playbook that migrations succeeded when the database was merely unreachable,
    # and the playbook would go on to restart services against an unmigrated schema. Silent
    # success is the one outcome this tool must never produce, so every non-SystemExit failure
    # is converted into a non-zero exit here, inside the service, without touching runner.py.
    # SystemExit derives from BaseException, so the happy-path exit below is not re-wrapped.
    code = 0
    try:
        # One connection per target, processed strictly in sequence: two targets can point at
        # the same physical database (e.g. a dev box collapsing "main" and "gis"), and the
        # advisory lock tracker.lock() takes would contend with itself under concurrency.
        for target in selected:
            logger.debug(f"migrations: target={target.name} dir={target.directory} table={target.table}")
            out = _out_for(target.name, multi)

            async with await psycopg.AsyncConnection.connect(**connect_kwargs(config["db"][target.db])) as conn:
                if args.step == "status":
                    target_code = await cmd_status(conn, target.directory, target.table, args.check, out)
                elif args.step == "apply":
                    dry_run = getattr(args, "dry_run", False)
                    target_code = await cmd_apply(
                        conn, target.directory, target.table, dry_run, args.to, applied_by, out
                    )
                elif args.step == "baseline":
                    target_code = await cmd_baseline(
                        conn, target.directory, target.table, args.to, args.yes, applied_by, input, out
                    )
                elif args.step == "repair":
                    target_code = await cmd_repair(conn, target.directory, target.table, args.name, out)
                else:
                    out(f"error: unknown migrations command {args.step!r}")
                    target_code = 1

            if args.step == "apply":
                # Stop at the first failing target: a broken `main` must never leave a
                # deploy half-migrated across two databases by ploughing on to the next one.
                code = target_code
                if code != 0:
                    break
            elif args.step == "status":
                # Every target is checked regardless of earlier results: `--check` must
                # report on all of them, not just the first failure.
                code = code or target_code
            else:
                code = target_code

    except Exception:
        logger.exception("migrations: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)
