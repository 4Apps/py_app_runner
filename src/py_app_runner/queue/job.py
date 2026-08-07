"""The job as a handler sees it, and the errors the queue raises."""

from dataclasses import dataclass, field
from typing import Any


class QueueError(Exception):
    """A queue that is configured or called wrongly. Never raised for a job that failed -
    that is what the failed table is for."""


@dataclass
class Job:
    id: int
    queue: str
    name: str
    payload: dict[str, Any]
    # The row exactly as it was written. Carried so a job moved to the failed table keeps
    # byte-identical payload, rather than a re-encoding of a decoding of it.
    payload_json: str
    # Includes this attempt: reserving *is* the attempt, so the first run sees 1.
    attempts: int
    max_attempts: int
    # Driver bookmark. Empty on the database driver, the stream entry id on redis.
    handle: str = ""

    _release_delay: int | None = field(default=None, repr=False)

    def release(self, delay: int = 0) -> None:
        """Put this job back for another attempt.

        A deliberate "not now" - a rate limit upstream, a file that has not landed yet -
        rather than a failure. It does not count against the attempt budget any differently
        from a crash, because the claim already counted it.
        """

        self._release_delay = max(0, delay)

    def was_released(self) -> bool:
        return self._release_delay is not None

    def release_delay(self) -> int:
        return self._release_delay or 0

    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts
