"""What both drivers answer to.

A Protocol rather than a base class, so neither driver inherits anything it does not use and
a test double does not have to subclass to stand in for one.

The shapes are fixed to the database driver's vocabulary - `pending`, `delayed`, `reserved`
- even where redis models the same idea differently. There is one status command and it
prints one table, so the driver is what adapts.
"""

from typing import Any, Protocol, runtime_checkable

from py_app_runner.queue.job import Job


@runtime_checkable
class QueueDriver(Protocol):
    async def push(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        delay: int = 0,
        queue: str = "default",
        priority: int = 0,
        unique: str | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Queue a job, returning its id - or the id of the job already holding `unique`."""
        ...

    async def reserve(self, queues: list[str], timeout: int, worker: str) -> Job | None:
        """Claim the next due job, or None. `queues` is a precedence order, not a merged
        sort. The claim lasts `timeout` seconds and must become claimable again after that
        without anybody having to release it."""
        ...

    async def delete(self, job: Job) -> None:
        """Done. Forget it."""
        ...

    async def release(self, job: Job, delay: int = 0, error: str = "") -> None:
        """Put it back for another attempt. Must not change `attempts` - reserving already
        counted this one."""
        ...

    async def fail(self, job: Job, error: str) -> None:
        """Out of attempts. Keep it where a human will find it."""
        ...

    async def pending(self, queue: str | None = None) -> int:
        """How many could be picked up right now: excludes jobs not yet due and jobs another
        worker currently holds."""
        ...

    async def stats(self) -> list[dict[str, Any]]: ...

    async def failed_count(self) -> int: ...

    async def failed_rows(self, limit: int) -> list[dict[str, Any]]: ...

    async def retry_failed(self, job_id: int | None, max_attempts: int) -> int: ...

    async def forget_failed(self, job_id: int | None, before: str | None) -> int: ...
