"""Rate limiting: count attempts against a key, and say when the caller may try again.

Fixed window rather than sliding: the window opens on the first hit and closes `window`
seconds later, whatever happens in between. The known cost is the boundary - a client can
spend a full allowance at the end of one window and another at the start of the next, so
for a limit of 5 per 15 minutes the true worst case is 10 in quick succession. That is
documented here rather than left to be discovered, and it is the right trade for the thing
this protects: a login form, not a billing meter.

    attempt = await throttle.hit(f"login:{email}", 5, 900)
    if not attempt.allowed:
        raise HTTPException("Too many attempts", code=4029, http_status=429)

    # ... and the moment the protected thing succeeds:
    await throttle.clear(f"login:{email}")

Clearing on success is what stops a user who mistyped their password four times and then
got it right from staying one attempt away from a lockout for the rest of the window.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

_DEFAULT_PREFIX = "throttle:"

# One round trip, and exact. Read-modify-write over separate commands would let simultaneous
# requests read the same count and each let an extra attempt through; a script costs the same
# latency and has no such window, so there is no reason to accept the race.
#
# The reset timestamp is stored rather than derived from the key's TTL. It is what decides
# whether a window is over, so it has to survive a backend that dropped the expiry - which
# is exactly what a crash between a SET and its EXPIRE leaves behind.
_HIT_SCRIPT = """
local hits = tonumber(redis.call('HGET', KEYS[1], 'hits'))
local reset = tonumber(redis.call('HGET', KEYS[1], 'reset'))
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

if hits == nil or reset == nil or reset <= now then
    hits = 0
    reset = now + window
end

hits = hits + 1

redis.call('HSET', KEYS[1], 'hits', hits, 'reset', reset)
redis.call('EXPIRE', KEYS[1], math.max(1, reset - now))

return {hits, reset}
"""


@dataclass(frozen=True)
class Attempt:
    """The state of one key after an attempt.

    A value object rather than a bool because every caller that denies a request also has
    to tell the client when to come back, and a bool makes that a second lookup.
    """

    allowed: bool
    limit: int
    hits: int
    remaining: int
    retry_after: int
    reset_at: int

    def headers(self) -> dict[str, str]:
        """The standard advertisement headers. `Retry-After` only when denied - sending it
        on a successful response tells a well-behaved client to back off when it need not."""

        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            # An absolute unix timestamp, not a delta: a delta computed at send time is
            # already wrong by the time it is read.
            "X-RateLimit-Reset": str(self.reset_at),
        }

        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)

        return headers


class Throttle:
    def __init__(self, redis_con: Any, prefix: str = _DEFAULT_PREFIX, fail_open: bool = True) -> None:
        self.redis_con = redis_con
        self.prefix = prefix
        # Redis being unreachable is an outage of the cache, not of the application.
        # Refusing every login until it comes back turns a degraded dependency into a total
        # one, so the default is to allow and log. It is a choice, not a silence.
        self.fail_open = fail_open

    @classmethod
    def from_config(cls, redis_con: Any, config: dict[str, Any]) -> "Throttle":
        settings = config.get("throttle") or {}
        return cls(
            redis_con=redis_con,
            prefix=settings.get("prefix") or _DEFAULT_PREFIX,
            fail_open=settings.get("fail_open") is not False,
        )

    def cache_key(self, key: str) -> str:
        """Hash the caller's key.

        It is routinely an email address or an IP, and a shared Redis keyspace is readable
        by anything else that connects to it. Hashing costs nothing here and means the
        keyspace is not a list of who has been failing to log in.
        """

        return self.prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def hit(self, key: str, max_attempts: int, window: int) -> Attempt:
        """Count one attempt and report the result.

        The attempt is counted whether or not it is allowed, so hammering neither resets
        the window nor extends it.
        """

        self._assert_limit(max_attempts, window)
        now = int(time.time())

        try:
            hits, reset_at = await self._run_hit(key, now, window)
        except Exception as e:
            return self._unavailable(e, max_attempts, now, window)

        return self._attempt(hits, reset_at, max_attempts, now)

    async def check(self, key: str, max_attempts: int) -> Attempt:
        """Peek without counting. Use it to show a client where it stands."""

        self._assert_limit(max_attempts, 1)
        now = int(time.time())

        try:
            stored = await self.redis_con.hmget(self.cache_key(key), "hits", "reset")
        except Exception as e:
            return self._unavailable(e, max_attempts, now, 0)

        hits, reset_at = self._parse(stored)
        if hits is None or reset_at is None or reset_at <= now:
            # Nothing stored, or a window that has closed: a full allowance. reset_at is
            # `now` rather than `now + window` because check() is not told the window
            # length and must not invent one.
            return Attempt(True, max_attempts, 0, max_attempts, 0, now)

        return self._attempt(hits, reset_at, max_attempts, now)

    async def clear(self, key: str) -> None:
        """Forget a key. Call it the moment the protected thing succeeds."""

        try:
            await self.redis_con.delete(self.cache_key(key))
        except Exception as e:
            if not self.fail_open:
                raise

            _logger.warning("throttle: could not clear a counter: %s", e)

    ###############
    ### Interna ###
    ###############

    async def _run_hit(self, key: str, now: int, window: int) -> tuple[int, int]:
        result = await self.redis_con.eval(_HIT_SCRIPT, 1, self.cache_key(key), now, window)
        return (int(result[0]), int(result[1]))

    def _attempt(self, hits: int, reset_at: int, max_attempts: int, now: int) -> Attempt:
        # Increment-then-compare, so hits == max is the last allowed attempt and
        # hits == max + 1 is the first denial.
        allowed = hits <= max_attempts

        return Attempt(
            allowed=allowed,
            limit=max_attempts,
            hits=hits,
            remaining=max(0, max_attempts - hits),
            retry_after=0 if allowed else max(0, reset_at - now),
            reset_at=reset_at,
        )

    def _parse(self, stored: Any) -> tuple[int | None, int | None]:
        """Read stored state, treating anything unrecognisable as absent.

        A key of this name holding something else - a leftover from another tool, a
        half-written value - starts a fresh window rather than raising. Throwing here would
        take down the login form over a stray cache entry.
        """

        if not stored or len(stored) != 2:
            return (None, None)

        try:
            return (int(stored[0]), int(stored[1]))
        except (TypeError, ValueError):
            return (None, None)

    def _unavailable(self, error: Exception, max_attempts: int, now: int, window: int) -> Attempt:
        if not self.fail_open:
            raise error

        _logger.warning("throttle: %s", error)
        return Attempt(True, max_attempts, 0, max_attempts, 0, now + window)

    def _assert_limit(self, max_attempts: int, window: int) -> None:
        if max_attempts < 1:
            raise ValueError(f"A throttle limit is at least 1 attempt; got {max_attempts}.")

        if window < 1:
            raise ValueError(f"A throttle window is at least 1 second; got {window}.")
