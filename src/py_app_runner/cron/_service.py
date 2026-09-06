import asyncio
import datetime
import logging
import os
import signal
import sys
from argparse import Namespace
from typing import Any

import psycopg

from py_app_runner.cron.commands import Out, cmd_list, cmd_run, cmd_work, select_due
from py_app_runner.cron.execute import Executor, LocalLock, Lock, PgLock, install_shutdown_handlers
from py_app_runner.cron.schedule import CronError, Job, current_minute, load_jobs, settings
from py_app_runner.migrations._service import connect_kwargs
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry


def app_argv(args: Namespace) -> list[str]:
    """The interpreter and script this scheduler itself was started with, so a child is the
    same app.py from the same cwd with the same .env, at the scheduler's log level."""

    return [sys.executable, os.path.abspath(sys.argv[0]), "-v", getattr(args, "v", None) or "info"]


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    out: Out = print

    # runner.py catches Exception around init_service and returns normally, which exits 0.
    # A tick that could not even read its config must not look like a quiet minute.
    code = 1
    try:
        config = AppRegistry.config()
        conf = settings(config)
        jobs = load_jobs(config)

        if args.step == "list":
            raise SystemExit(cmd_list(jobs, datetime.datetime.now(datetime.UTC), out))

        if args.step == "work":
            raise SystemExit(await _work(args, logger))

        if args.step != "run":
            out(f"error: unknown cron command {args.step!r}")
            raise SystemExit(1)

        due = select_due(jobs, current_minute(), args.job)
        if args.job is not None and not due:
            out(f"error: no job {args.job!r}; configured are: {', '.join(job.name for job in jobs) or 'none'}.")
            raise SystemExit(2)

        if getattr(args, "dry_run", False) or not due:
            # Neither path starts a process, so no lock and no database connection.
            raise SystemExit(await cmd_run(due, _executor(args, LocalLock(), logger), True, logger, out))

        async with await psycopg.AsyncConnection.connect(**connect_kwargs(_db_config(conf, config, out))) as conn:
            executor = _executor(args, PgLock(conn), logger)
            install_shutdown_handlers(executor, logger)
            code = await cmd_run(due, executor, False, logger, out)

    except CronError as e:
        out(f"error: {e}")
        raise SystemExit(2) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Above `except Exception` because both derive from BaseException.
        logger.error("cron: interrupted")
        raise SystemExit(1) from None
    except Exception:
        logger.exception("cron: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)


def _db_config(conf: dict[str, Any], config: dict[str, Any], out: Out) -> dict[str, Any]:
    db_name = conf["db"]
    db_config = config.get("db") or {}
    if db_name not in db_config:
        out(
            f'error: no database {db_name!r} in config["db"]; configured are: {", ".join(sorted(db_config)) or "none"}.'
        )
        raise SystemExit(2)

    return db_config[db_name]


def _executor(args: Namespace, lock: Lock, logger: logging.Logger) -> Executor:
    base = app_argv(args)

    def launch(job: Job) -> list[str]:
        return [*base, *job.command]

    return Executor(launch, lock, logger)


async def _work(args: Namespace, logger: logging.Logger) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, name), stop.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            logger.debug(f"cron: could not install a {name} handler; shutdown will not be graceful")

    logger.info("cron: worker started, ticking every minute")
    return await cmd_work([*app_argv(args), "cron", "run"], stop, logger)
