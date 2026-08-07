"""The worker loop.

Polls rather than blocks, so latency is bounded by `sleep` and nothing holds a connection
open waiting. One reserve, one job, one completion per iteration.

The per-job timeout is `asyncio.timeout()`, which genuinely cancels the task rather than
asking it to stop at a point it may never reach - a signal-based timeout cannot interrupt a
job blocked in a query, which is the case most worth interrupting. The visibility timeout
stays as the backstop for a worker that dies outright rather than merely overrunning.
"""

import asyncio
import logging
import time
import traceback
from collections.abc import Callable
from typing import Any

from py_app_runner.queue.handler import resolve
from py_app_runner.queue.job import Job

Out = Callable[[str], None]

_logger = logging.getLogger(__name__)

# Five consecutive failures to reserve means the queue is unreadable, not that it is empty.
# Exiting lets a supervisor restart the process rather than having it sit there logging.
MAX_CONSECUTIVE_FAILURES = 5


def backoff(attempt: int, steps: list[int] | int | None = None) -> int:
    """Delay before the next attempt, given how many have already been made.

    A list gives one delay per attempt and repeats the last entry forever, so a job that
    keeps failing settles at a sensible interval rather than growing without bound.
    """

    if steps is None:
        steps = [10, 60, 300]

    if isinstance(steps, int):
        return max(0, steps)

    if not steps:
        return 0

    return max(0, steps[min(max(0, attempt - 1), len(steps) - 1)])


class Worker:
    def __init__(
        self,
        queue: Any,
        handlers: dict[str, Any] | None = None,
        backoff_steps: list[int] | int | None = None,
        out: Out | None = None,
        worker_id: str = "worker",
    ) -> None:
        self.queue = queue
        self.handlers = handlers or {}
        self.backoff_steps = backoff_steps
        self.out = out or print
        self.worker_id = worker_id
        self.should_quit = False

    def stop(self) -> None:
        """Ask the loop to finish the job in hand and exit."""

        self.should_quit = True

    async def run(
        self,
        queues: list[str],
        timeout: int = 300,
        sleep: float = 1.0,
        max_jobs: int = 0,
        max_time: int = 0,
        stop_when_empty: bool = False,
    ) -> int:
        """Returns a process exit code: 0 for every ordinary end, 1 only when the queue
        could not be read repeatedly. A failed *job* never changes the exit code - that is
        what the failed table is for."""

        self.out(f"Worker {self.worker_id} watching {', '.join(queues)}")

        started = time.monotonic()
        done = 0
        failures = 0

        while True:
            if self.should_quit:
                self.out("Stopping: asked to shut down")
                break

            try:
                job = await self.queue.reserve(queues, timeout, self.worker_id)
                failures = 0
            except Exception as e:
                failures += 1
                self.out(f"error: could not reserve a job: {e}")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    self.out(f"Stopping: the queue has been unreadable {MAX_CONSECUTIVE_FAILURES} times running")
                    return 1

                await self._rest(sleep)
                continue

            if job is None:
                if stop_when_empty:
                    self.out("Nothing left to do")
                    break

                reason = self._limit_reached(done, started, max_jobs, max_time)
                if reason:
                    self.out(f"Stopping: {reason}")
                    break

                await self._rest(sleep)
                continue

            await self._run_job(job, timeout)
            done += 1

            reason = self._limit_reached(done, started, max_jobs, max_time)
            if reason:
                self.out(f"Stopping: {reason}")
                break

        self.out(f"Ran {done} job(s)")
        return 0

    async def run_next(self, queues: list[str], timeout: int = 300) -> bool:
        """Reserve and run exactly one job. The single-shot primitive, for tests - it
        installs no signal handlers, applies no limits and does not catch reserve failures."""

        job = await self.queue.reserve(queues, timeout, self.worker_id)
        if job is None:
            return False

        await self._run_job(job, timeout)
        return True

    ###############
    ### Running ###
    ###############

    async def _run_job(self, job: Job, timeout: int) -> None:
        self.out(f"-> {job.name} #{job.id} (attempt {job.attempts}/{job.max_attempts})")
        started = time.monotonic()

        try:
            # Resolution is inside the try on purpose: a handler that cannot be built fails
            # the job through the same release/fail path as one that threw, rather than
            # being a separate outcome nobody handles.
            handler = resolve(job.name, self.handlers)
            async with asyncio.timeout(timeout):
                await handler(job.payload, job)
        except asyncio.CancelledError:
            # A cancellation that is not ours - the process is shutting down. Release the
            # job so it is picked up promptly rather than waiting out its reservation, and
            # let the cancellation continue.
            await self._safe_release(job, 0, "Worker was cancelled mid-job")
            raise
        except Exception as e:
            # QueueError included: a handler that cannot be resolved is a job that cannot
            # run, and it goes through the same release/fail budget as any other failure.
            await self._job_failed(job, e)
            return

        if job.was_released():
            await self.queue.release(job, job.release_delay(), "")
            self.out(f"   released, back in {job.release_delay()}s")
            return

        await self.queue.delete(job)
        self.out(f"   done in {int((time.monotonic() - started) * 1000)}ms")

    async def _job_failed(self, job: Job, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}\n{''.join(traceback.format_exception(error))}"

        if job.is_last_attempt():
            await self.queue.fail(job, detail)
            self.out(f"   failed for good: {error}")
            # Both, deliberately: process output goes to a supervisor log nobody reads,
            # and the application's own logging is where the rest of its problems surface.
            _logger.error("Queue job %s #%s failed permanently: %s", job.name, job.id, error)
            return

        delay = backoff(job.attempts, self.backoff_steps)
        await self.queue.release(job, delay, detail)
        self.out(f"   failed, retrying in {delay}s: {error}")

    async def _safe_release(self, job: Job, delay: int, error: str) -> None:
        try:
            await self.queue.release(job, delay, error)
        except Exception as e:
            # Already unwinding; the reservation will expire on its own.
            _logger.warning("Queue could not release job #%s during shutdown: %s", job.id, e)

    ###############
    ### Limits ####
    ###############

    def _limit_reached(self, done: int, started: float, max_jobs: int, max_time: int) -> str:
        if max_jobs > 0 and done >= max_jobs:
            return f"ran {done} jobs"

        if max_time > 0 and (time.monotonic() - started) >= max_time:
            return f"been going for {int(time.monotonic() - started)}s"

        return ""

    async def _rest(self, sleep: float) -> None:
        """Sleep in short slices so a shutdown request is noticed promptly rather than after
        a full poll interval."""

        remaining = sleep
        while remaining > 0 and not self.should_quit:
            slice_length = min(0.25, remaining)
            await asyncio.sleep(slice_length)
            remaining -= slice_length
