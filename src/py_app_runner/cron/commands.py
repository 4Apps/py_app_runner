"""Command implementations, importable directly for programmatic use."""

import asyncio
import datetime
import logging
import shlex
from collections.abc import Callable

from py_app_runner.cron.execute import Executor
from py_app_runner.cron.schedule import Job

Out = Callable[[str], None]


def cmd_list(jobs: list[Job], now: datetime.datetime, out: Out) -> int:
    if not jobs:
        out("No jobs configured.")
        return 0

    width = max(len(job.name) for job in jobs)
    out(f"{'job':<{width}}  {'schedule':<16} {'next run':<25} command")
    for job in jobs:
        next_run = job.next_run(now).strftime("%Y-%m-%d %H:%M %Z")
        out(f"{job.name:<{width}}  {job.schedule:<16} {next_run:<25} {shlex.join(job.command)}")

    return 0


def select_due(jobs: list[Job], minute: datetime.datetime, only: str | None) -> list[Job]:
    """`--job NAME` means now, whatever the schedule says: it is the operator's way of
    firing a job by hand, and the point of the exercise is to run it."""

    if only is not None:
        return [job for job in jobs if job.name == only]

    return [job for job in jobs if job.is_due(minute)]


async def cmd_run(
    due: list[Job],
    executor: Executor,
    dry_run: bool,
    logger: logging.Logger,
    out: Out,
) -> int:
    if not due:
        logger.debug("cron: nothing due")
        return 0

    if dry_run:
        for job in due:
            out(f"would run {job.name}: {shlex.join(job.command)}")

        return 0

    outcomes = await executor.run(due)

    # A job that was skipped for still running is the lock doing its job, not a failure.
    return 1 if any(o.status in ("failed", "timeout") for o in outcomes) else 0


async def cmd_work(tick_argv: list[str], stop: asyncio.Event, logger: logging.Logger) -> int:
    """Each minute, one `cron run` child; it does the lock and the waiting. The loop itself
    never blocks on a job, so a slow one cannot delay the next tick for the others."""

    ticks: list[asyncio.subprocess.Process] = []

    while not stop.is_set():
        await _wait_for_minute(stop)
        if stop.is_set():
            break

        logger.debug("cron: tick")
        try:
            ticks.append(await asyncio.create_subprocess_exec(*tick_argv))
        except OSError as e:
            logger.error(f"cron: could not start a tick: {e}")
            return 1

        ticks = [proc for proc in ticks if proc.returncode is None]

    for proc in ticks:
        if proc.returncode is None:
            proc.terminate()

    await asyncio.gather(*(proc.wait() for proc in ticks))
    return 0


async def _wait_for_minute(stop: asyncio.Event) -> None:
    now = datetime.datetime.now(datetime.UTC)
    next_minute = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)

    try:
        await asyncio.wait_for(stop.wait(), (next_minute - now).total_seconds())
    except TimeoutError:
        pass
