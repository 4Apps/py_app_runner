"""The executor against real child processes: every job is a `python -c` snippet, so
what is checked is process handling - exit codes, the overlap lock, the timeout kill -
not any particular app.py."""

import asyncio
import datetime
import logging
import sys

from py_app_runner.cron.commands import cmd_run, select_due
from py_app_runner.cron.execute import Executor, LocalLock
from py_app_runner.cron.schedule import Job


def job(name: str, code: str, timeout: int | None = None) -> Job:
    return Job(name=name, schedule="* * * * *", command=(code,), timezone=datetime.UTC, timeout=timeout)


def launch(j: Job) -> list[str]:
    return [sys.executable, "-c", j.command[0]]


def executor(lock=None) -> Executor:
    return Executor(launch, lock or LocalLock(), logging.getLogger("test"))


class TestExecutor:
    async def test_success_and_failure_are_reported_per_job(self):
        outcomes = await executor().run([job("ok", "pass"), job("bad", "raise SystemExit(3)")])

        by_name = {o.job.name: o for o in outcomes}
        assert by_name["ok"].status == "ok"
        assert by_name["ok"].returncode == 0
        assert by_name["bad"].status == "failed"
        assert by_name["bad"].returncode == 3

    async def test_jobs_run_concurrently(self):
        started = asyncio.get_running_loop().time()
        outcomes = await executor().run([job(f"s{i}", "import time; time.sleep(0.5)") for i in range(3)])

        assert all(o.status == "ok" for o in outcomes)
        assert asyncio.get_running_loop().time() - started < 1.2

    async def test_locked_job_is_skipped_without_starting(self):
        lock = LocalLock()
        assert await lock.acquire("busy")

        [outcome] = await executor(lock).run([job("busy", "raise SystemExit(9)")])

        assert outcome.status == "skipped"
        assert outcome.returncode is None

    async def test_lock_is_released_after_the_run(self):
        lock = LocalLock()

        await executor(lock).run([job("a", "raise SystemExit(1)")])

        assert await lock.acquire("a")

    async def test_timeout_kills_the_child(self):
        [outcome] = await executor().run([job("slow", "import time; time.sleep(30)", timeout=1)])

        assert outcome.status == "timeout"
        assert outcome.returncode not in (None, 0)
        assert outcome.seconds < 5

    async def test_unstartable_command_is_a_failure(self):
        ex = Executor(lambda j: ["/nonexistent/binary"], LocalLock(), logging.getLogger("test"))

        [outcome] = await ex.run([job("nope", "pass")])

        assert outcome.status == "failed"


class TestCmdRun:
    async def test_exit_code_follows_the_worst_outcome(self):
        logger = logging.getLogger("test")
        quiet = lambda _: None  # noqa: E731
        mixed = [job("ok", "pass"), job("bad", "raise SystemExit(1)")]

        assert await cmd_run([job("ok", "pass")], executor(), False, logger, quiet) == 0
        assert await cmd_run(mixed, executor(), False, logger, quiet) == 1

    async def test_skipped_is_not_a_failure(self):
        lock = LocalLock()
        await lock.acquire("busy")

        logger = logging.getLogger("test")

        assert await cmd_run([job("busy", "pass")], executor(lock), False, logger, lambda _: None) == 0

    async def test_dry_run_prints_and_starts_nothing(self):
        lines: list[str] = []

        logger = logging.getLogger("test")

        code = await cmd_run([job("bad", "raise SystemExit(1)")], executor(), True, logger, lines.append)

        assert code == 0
        assert lines == ["would run bad: 'raise SystemExit(1)'"]

    def test_select_due_by_name_ignores_the_schedule(self):
        never = Job(name="never", schedule="0 0 29 2 *", command=("x",), timezone=datetime.UTC, timeout=None)
        minute = datetime.datetime(2026, 9, 7, 10, 0, tzinfo=datetime.UTC)

        assert select_due([never], minute, None) == []
        assert select_due([never], minute, "never") == [never]
        assert select_due([never], minute, "other") == []
