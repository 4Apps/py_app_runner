import asyncio
import logging
import os
import pathlib
import signal
import socket
from argparse import Namespace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from database_wrapper_redis import RedisDbWithPoolAsync

from py_app_runner.migrations._service import connect_kwargs, resolve_targets
from py_app_runner.pybridge import PyBridge
from py_app_runner.queue.commands import (
    Out,
    cmd_failed,
    cmd_forget,
    cmd_install,
    cmd_retry,
    cmd_status,
    parse_queues,
    resolve_handlers,
)
from py_app_runner.queue.driver_pg import PgQueue
from py_app_runner.queue.driver_redis import RedisQueue
from py_app_runner.queue.interface import QueueDriver
from py_app_runner.queue.job import QueueError
from py_app_runner.queue.worker import Worker
from py_app_runner.registry import AppRegistry

_DEFAULTS: dict[str, Any] = {
    # "database" first because a push there joins the caller's transaction, which is the
    # one thing redis cannot offer however fast it is.
    "driver": "database",
    "db": "main",
    "table": "queue_jobs",
    "failed_table": "queue_failed_jobs",
    # Deliberately not the cache connection: a queue that shares a database with a cache is
    # one FLUSHDB away from an empty backlog.
    "redis": {},
    "queue": "default",
    "tries": 3,
    "backoff": [10, 60, 300],
    "timeout": 300,
    "sleep": 1.0,
    "handlers": {},
}


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    return {**_DEFAULTS, **(config.get("queue") or {})}


def _migrations_dir(args: Namespace, config: dict[str, Any]) -> pathlib.Path:
    raw = getattr(args, "dir", None)
    if raw:
        directory = pathlib.Path(raw)
        if directory.is_absolute():
            return directory

        return pathlib.Path(config.get("current_path") or ".") / directory

    return next(iter(resolve_targets(config).values())).directory


def _install_shutdown_handlers(worker: Worker, logger: logging.Logger) -> None:
    """SIGTERM and SIGINT ask the loop to stop; nothing interrupts the job in hand.

    Installed on the running loop rather than through signal.signal, so the flag is set
    between awaits rather than inside arbitrary C code. A second signal is left to the
    default handler: if the first one is being ignored because a job is wedged, the
    operator needs a way out that does not require finding the pid.
    """

    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, name), worker.stop)
        except (NotImplementedError, AttributeError, RuntimeError):
            # No signal support (Windows, or a non-main thread). The visibility timeout is
            # still the backstop, so a killed worker's jobs come back on their own.
            logger.debug("queue: could not install a %s handler; shutdown will not be graceful", name)


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    out: Out = print

    # Same reasoning as migrations: runner.py catches Exception around init_service and
    # returns normally, which exits 0. A worker that could not reach the database must not
    # look like a worker that ran cleanly.
    code = 1
    try:
        config = AppRegistry.config()
        settings = _settings(config)

        driver = settings["driver"]
        if driver not in ("database", "redis"):
            out(f'error: config["queue"]["driver"] is {driver!r}; it must be "database" or "redis".')
            raise SystemExit(2)

        if args.step == "install":
            if driver == "redis":
                # Nothing to create: the keys appear on first push. Exiting 0 rather than
                # refusing, so a deploy playbook can run `queue install` unconditionally
                # whichever driver a project happens to be on.
                out("Nothing to install: this queue is not on a database.")
                raise SystemExit(0)

            raise SystemExit(
                cmd_install(
                    _migrations_dir(args, config),
                    settings["table"],
                    settings["failed_table"],
                    datetime.now(UTC),
                    out,
                )
            )

        if driver == "redis":
            # No database connection is opened at all on this path, so a worker host running
            # the redis driver needs no database credentials.
            #
            # `prefix` and `group` are this module's keys and are not part of the connector's
            # config, so they are split out rather than handed to it.
            redis_settings = dict(settings["redis"] or {})
            prefix = redis_settings.pop("prefix", None) or "queue:"
            group = redis_settings.pop("group", None) or "workers"

            pool = RedisDbWithPoolAsync(cast(Any, redis_settings))
            try:
                async with pool as redis_con:
                    queue = RedisQueue(redis_con, prefix, group)
                    code = await _dispatch(args, settings, config, queue, logger, out)
            finally:
                await pool.close()

            raise SystemExit(code)

        db_name = settings["db"]
        db_config = config.get("db") or {}
        if db_name not in db_config:
            out(
                f'error: no database {db_name!r} in config["db"]; '
                f"configured are: {', '.join(sorted(db_config)) or 'none'}."
            )
            raise SystemExit(2)

        async with await psycopg.AsyncConnection.connect(**connect_kwargs(db_config[db_name])) as conn:
            code = await _dispatch(
                args, settings, config, PgQueue(conn, settings["table"], settings["failed_table"]), logger, out
            )

    except QueueError as e:
        out(f"error: {e}")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Above `except Exception` because both derive from BaseException. A worker
        # interrupted mid-job leaves that job reserved until its deadline passes, which is
        # correct - but it must not be reported as a clean run.
        logger.error("queue: interrupted")
        raise SystemExit(1) from None
    except Exception:
        logger.exception("queue: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)


async def _dispatch(
    args: Namespace,
    settings: dict[str, Any],
    config: dict[str, Any],
    queue: QueueDriver,
    logger: logging.Logger,
    out: Out,
) -> int:
    """Every command below is driver-agnostic, which is the point of the interface: switching
    `driver` in config changes where the jobs live and nothing else about the CLI."""

    if args.step == "work":
        return await _work(args, settings, config, queue, logger, out)

    if args.step == "status":
        return await cmd_status(queue, out)

    if args.step == "failed":
        return await cmd_failed(queue, args.limit, out)

    if args.step == "retry":
        return await cmd_retry(queue, args.id, args.all, settings["tries"], out)

    if args.step == "forget":
        return await cmd_forget(queue, args.id, args.all, args.before, out)

    out(f"error: unknown queue command {args.step!r}")
    return 1


async def _work(
    args: Namespace,
    settings: dict[str, Any],
    config: dict[str, Any],
    queue: QueueDriver,
    logger: logging.Logger,
    out: Out,
) -> int:
    worker = Worker(
        queue=queue,
        handlers=resolve_handlers(config),
        backoff_steps=settings["backoff"],
        out=out,
        worker_id=f"{socket.gethostname()}:{os.getpid()}"[:64],
    )

    _install_shutdown_handlers(worker, logger)

    # --once is not run_next(): it is a full run with the limits set, so it still installs
    # signal handlers and still reports what it did.
    max_jobs = 1 if args.once else args.max_jobs
    stop_when_empty = args.stop_when_empty or args.once

    return await worker.run(
        queues=parse_queues(args.queue, settings["queue"]),
        timeout=args.timeout if args.timeout is not None else settings["timeout"],
        sleep=args.sleep if args.sleep is not None else settings["sleep"],
        max_jobs=max_jobs,
        max_time=args.max_time,
        stop_when_empty=stop_when_empty,
    )
