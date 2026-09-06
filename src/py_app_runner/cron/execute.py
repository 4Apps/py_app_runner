"""Running due jobs as child processes.

Each job is a fresh `app.py <command>` process, so a job that leaks, wedges or crashes
takes nothing but itself down, and `timeout` can be enforced by killing it. The scheduler
holds an overlap lock per job for as long as the child runs; the next tick sees the lock
and skips instead of starting a second copy.
"""

import asyncio
import logging
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import psycopg

from py_app_runner.cron.schedule import Job

# Advisory locks in the two-int form live in their own keyspace, so this cannot collide
# with the single-bigint key the migrations tracker takes.
ADVISORY_NAMESPACE = 0x4A0B
KILL_GRACE = 10.0

Launch = Callable[[Job], list[str]]


class Lock(Protocol):
    async def acquire(self, name: str) -> bool: ...

    async def release(self, name: str) -> None: ...


class PgLock:
    """Session-level advisory lock, so a scheduler that dies drops its locks with the
    connection. The child it was waiting on is orphaned rather than killed, which is why
    the next tick may still find the job running - the job itself has to be safe to
    overlap in that one case."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def acquire(self, name: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", (ADVISORY_NAMESPACE, name))
            row = await cur.fetchone()

        return bool(row and row[0])

    async def release(self, name: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (ADVISORY_NAMESPACE, name))


class LocalLock:
    """In-process lock for a scheduler that is the only one by construction."""

    def __init__(self) -> None:
        self._held: set[str] = set()

    async def acquire(self, name: str) -> bool:
        if name in self._held:
            return False

        self._held.add(name)
        return True

    async def release(self, name: str) -> None:
        self._held.discard(name)


@dataclass
class Outcome:
    job: Job
    status: str  # "ok", "failed", "timeout", "skipped"
    returncode: int | None = None
    seconds: float = 0.0


class Executor:
    def __init__(self, launch: Launch, lock: Lock, logger: logging.Logger) -> None:
        self._launch = launch
        self._lock = lock
        self._logger = logger
        self._children: dict[str, asyncio.subprocess.Process] = {}
        self._stopping = False

    def stop(self) -> None:
        """Forward the scheduler's own shutdown to whatever is still running. Called from a
        signal handler, so it only flags and signals; the awaiting run() sees the exit."""

        self._stopping = True
        for name, proc in self._children.items():
            if proc.returncode is None:
                self._logger.warning(f"cron: {name} interrupted")
                proc.terminate()

    async def run(self, jobs: Iterable[Job]) -> list[Outcome]:
        """Every job starts at once; the scheduler returns when the last one has ended."""

        return list(await asyncio.gather(*(self._run_one(job) for job in jobs)))

    async def _run_one(self, job: Job) -> Outcome:
        if not await self._lock.acquire(job.name):
            self._logger.warning(f"cron: {job.name} skipped, the previous run is still active")
            return Outcome(job, "skipped")

        try:
            return await self._spawn(job)
        finally:
            await self._lock.release(job.name)

    async def _spawn(self, job: Job) -> Outcome:
        argv = self._launch(job)
        started = time.monotonic()
        self._logger.info(f"cron: {job.name} started")

        try:
            proc = await asyncio.create_subprocess_exec(*argv)
        except OSError as e:
            self._logger.error(f"cron: {job.name} could not start: {e}")
            return Outcome(job, "failed")

        self._children[job.name] = proc
        try:
            try:
                await asyncio.wait_for(proc.wait(), job.timeout)
            except TimeoutError:
                await _kill(proc)
                seconds = time.monotonic() - started
                self._logger.error(f"cron: {job.name} killed after {seconds:.0f}s, over its {job.timeout}s timeout")
                return Outcome(job, "timeout", proc.returncode, seconds)
        finally:
            self._children.pop(job.name, None)

        seconds = time.monotonic() - started
        if proc.returncode == 0:
            self._logger.info(f"cron: {job.name} finished in {seconds:.1f}s")
            return Outcome(job, "ok", 0, seconds)

        # Being stopped is not the job's failure, but the run cannot be reported clean either.
        level = logging.WARNING if self._stopping else logging.ERROR
        self._logger.log(level, f"cron: {job.name} exited with {proc.returncode} after {seconds:.1f}s")
        return Outcome(job, "failed", proc.returncode, seconds)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def install_shutdown_handlers(executor: Executor, logger: logging.Logger) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, name), executor.stop)
        except (NotImplementedError, AttributeError, RuntimeError):
            logger.debug(f"cron: could not install a {name} handler; children will outlive a stop")
