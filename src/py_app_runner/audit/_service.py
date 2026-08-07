import asyncio
import logging
import pathlib
from argparse import Namespace
from datetime import UTC, datetime

import psycopg

from py_app_runner.audit.commands import Out, cmd_install, cmd_prune
from py_app_runner.audit.errors import AuditError
from py_app_runner.migrations._service import connect_kwargs, resolve_targets
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry

_DEFAULT_DB = "main"
_DEFAULT_TABLE = "audit_log"


def _migrations_dir(args: Namespace, config: dict) -> pathlib.Path:
    """Where `install` writes.

    The file it produces is a migration and belongs wherever the rest of them are, so the
    default comes from the migrations config rather than from an audit key of its own.
    """

    raw = getattr(args, "dir", None)
    if raw:
        directory = pathlib.Path(raw)
        if directory.is_absolute():
            return directory

        return pathlib.Path(config.get("current_path") or ".") / directory

    targets = resolve_targets(config)
    return next(iter(targets.values())).directory


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    out: Out = print

    # Same reasoning as migrations: runner.py catches Exception around init_service and
    # returns normally, which exits 0. A prune that could not reach the database must not
    # report success to a retention job.
    code = 1
    try:
        config = AppRegistry.config()
        settings = config.get("audit") or {}
        table = getattr(args, "table", None) or settings.get("table") or _DEFAULT_TABLE

        if args.step == "install":
            raise SystemExit(cmd_install(_migrations_dir(args, config), table, datetime.now(UTC), out))

        db_name = getattr(args, "db", None) or settings.get("db") or _DEFAULT_DB
        db_config = config.get("db") or {}
        if db_name not in db_config:
            out(
                f'error: no database {db_name!r} in config["db"]; '
                f"configured are: {', '.join(sorted(db_config)) or 'none'}."
            )
            raise SystemExit(2)

        if args.step == "prune":
            async with await psycopg.AsyncConnection.connect(**connect_kwargs(db_config[db_name])) as conn:
                code = await cmd_prune(
                    conn,
                    table,
                    args.before,
                    args.batch,
                    getattr(args, "dry_run", False),
                    out,
                )
        else:
            out(f"error: unknown audit command {args.step!r}")
            code = 1

    except AuditError as e:
        # Configuration and refusal messages already say what to do; a stack trace above
        # them would bury it.
        out(f"error: {e}")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Above `except Exception` because both derive from BaseException. An interrupted
        # prune has deleted some batches and not others - safe to resume, but it must not
        # be reported as complete.
        logger.error("audit: interrupted; the prune is partially done")
        raise SystemExit(1) from None
    except Exception:
        logger.exception("audit: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)
