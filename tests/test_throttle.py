import asyncio
import time

import pytest
import pytest_asyncio

from py_app_runner.throttle import Throttle
from tests.redis_harness import redis_for


@pytest_asyncio.fixture
async def redis_con():
    async with redis_for(database=2) as con:
        yield con


@pytest_asyncio.fixture
async def throttle(redis_con):
    return Throttle(redis_con)


class TestHit:
    async def test_the_first_hit_is_allowed_and_counted(self, throttle):
        attempt = await throttle.hit("login:one", 5, 900)

        assert attempt.allowed is True
        assert attempt.hits == 1
        assert attempt.remaining == 4
        assert attempt.limit == 5
        assert attempt.retry_after == 0

    async def test_counts_up_to_the_limit_then_denies(self, throttle):
        """Increment-then-compare, so hits == max is the last allowed attempt and
        hits == max + 1 is the first denial."""

        for expected_remaining in (2, 1, 0):
            attempt = await throttle.hit("login:two", 3, 900)
            assert attempt.allowed is True
            assert attempt.remaining == expected_remaining

        denied = await throttle.hit("login:two", 3, 900)
        assert denied.allowed is False
        assert denied.hits == 4
        assert denied.remaining == 0
        assert 0 < denied.retry_after <= 900

    async def test_counters_are_independent_per_key(self, throttle):
        await throttle.hit("login:a", 5, 900)
        await throttle.hit("login:a", 5, 900)
        other = await throttle.hit("login:b", 5, 900)

        assert other.hits == 1

    async def test_attempts_past_the_limit_do_not_extend_the_window(self, throttle):
        """Hammering must neither reset the window nor push it out, or a client that keeps
        trying could hold itself locked out indefinitely."""

        first = await throttle.hit("login:hammer", 1, 900)

        for _ in range(5):
            later = await throttle.hit("login:hammer", 1, 900)

        assert later.reset_at == first.reset_at

    async def test_an_expired_window_starts_over(self, throttle, redis_con):
        """Simulated by writing a reset in the past rather than sleeping. This also pins the
        stored shape, which is what the Lua script reads."""

        key = throttle.cache_key("login:expired")
        await redis_con.hset(key, mapping={"hits": 9, "reset": int(time.time()) - 1})

        attempt = await throttle.hit("login:expired", 5, 900)
        assert attempt.hits == 1
        assert attempt.allowed is True

    async def test_the_stored_reset_is_authoritative_not_the_ttl(self, throttle, redis_con):
        """A crash between a write and its EXPIRE leaves a key with no expiry. The window
        still has to end, so the decision is made on the stored timestamp rather than on
        whether Redis still holds the key."""

        key = throttle.cache_key("login:nottl")
        await throttle.hit("login:nottl", 5, 900)
        await redis_con.persist(key)

        assert await redis_con.ttl(key) == -1

        attempt = await throttle.hit("login:nottl", 5, 900)
        assert attempt.hits == 2
        assert await redis_con.ttl(key) > 0

    async def test_counting_is_exact_under_concurrency(self, throttle):
        """Read-modify-write over separate commands would let simultaneous requests read the
        same count and each let an extra attempt through. The Lua script costs the same one
        round trip and has no such window."""

        results = await asyncio.gather(*[throttle.hit("login:race", 100, 900) for _ in range(50)])

        assert sorted(a.hits for a in results) == list(range(1, 51))


class TestCheck:
    async def test_does_not_count(self, throttle):
        await throttle.hit("login:peek", 5, 900)

        first = await throttle.check("login:peek", 5)
        second = await throttle.check("login:peek", 5)

        assert first.hits == 1
        assert second.hits == 1

    async def test_an_unknown_key_reports_a_full_allowance(self, throttle):
        attempt = await throttle.check("login:never-seen", 5)

        assert attempt.allowed is True
        assert attempt.hits == 0
        assert attempt.remaining == 5


class TestClear:
    async def test_forgets_the_counter(self, throttle):
        """Call it the moment the protected thing succeeds, so a user who mistyped four
        times and then got it right is not one attempt from a lockout for the rest of the
        window."""

        for _ in range(3):
            await throttle.hit("login:clear", 5, 900)

        await throttle.clear("login:clear")

        assert (await throttle.hit("login:clear", 5, 900)).hits == 1

    async def test_clearing_an_unknown_key_is_not_an_error(self, throttle):
        await throttle.clear("login:never-existed")


class TestKeyPrivacy:
    async def test_the_callers_key_is_not_stored_in_the_clear(self, throttle, redis_con):
        """The key is routinely an email or an IP, and a shared Redis keyspace is readable
        by anything else that connects to it."""

        await throttle.hit("login:someone@example.com", 5, 900)

        keys = await redis_con.keys("*")
        assert keys
        assert not any("example.com" in key for key in keys)

    async def test_keys_carry_the_configured_prefix(self, redis_con):
        throttle = Throttle(redis_con, prefix="custom:")
        await throttle.hit("x", 5, 900)

        keys = await redis_con.keys("*")
        assert all(key.startswith("custom:") for key in keys)


class TestHeaders:
    async def test_an_allowed_attempt_advertises_the_allowance(self, throttle):
        headers = (await throttle.hit("login:h1", 5, 900)).headers()

        assert headers["X-RateLimit-Limit"] == "5"
        assert headers["X-RateLimit-Remaining"] == "4"
        assert "X-RateLimit-Reset" in headers
        # Sending Retry-After on a successful response tells a well-behaved client to back
        # off when it need not.
        assert "Retry-After" not in headers

    async def test_a_denied_attempt_says_when_to_come_back(self, throttle):
        await throttle.hit("login:h2", 1, 900)
        denied = await throttle.hit("login:h2", 1, 900)
        headers = denied.headers()

        assert headers["X-RateLimit-Remaining"] == "0"
        assert int(headers["Retry-After"]) > 0


class TestValidation:
    async def test_a_limit_below_one_is_refused(self, throttle):
        with pytest.raises(ValueError):
            await throttle.hit("x", 0, 900)

    async def test_a_window_below_one_second_is_refused(self, throttle):
        with pytest.raises(ValueError):
            await throttle.hit("x", 5, 0)


class Unreachable:
    """Stands in for a Redis that is down. Every call raises, which is what the client does
    once its connection pool gives up."""

    async def eval(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    async def hmget(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("redis is down")


class TestFailOpen:
    async def test_an_unreachable_backend_allows_by_default(self):
        """Redis being down is an outage of the cache, not of the application. Refusing
        every login until it returns turns a degraded dependency into a total one."""

        attempt = await Throttle(Unreachable()).hit("x", 5, 900)

        assert attempt.allowed is True
        assert attempt.remaining == 5

    async def test_fail_closed_propagates_when_asked(self):
        with pytest.raises(ConnectionError):
            await Throttle(Unreachable(), fail_open=False).hit("x", 5, 900)

    async def test_check_also_fails_open(self):
        assert (await Throttle(Unreachable()).check("x", 5)).allowed is True

    async def test_clear_does_not_raise_when_failing_open(self):
        await Throttle(Unreachable()).clear("x")
