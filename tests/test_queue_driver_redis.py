"""Redis-specific mechanics.

What a consumer group does with two readers, and what XAUTOCLAIM counts as idle, is exactly
what a hand written fake gets wrong - so these run against a real server. The
driver-agnostic behaviour is in test_queue_contract.py.
"""

import asyncio

import pytest_asyncio

from py_app_runner.queue.driver_redis import CONSUMER, RedisQueue
from tests.redis_harness import redis_for


@pytest_asyncio.fixture
async def redis_con():
    async with redis_for(database=5) as con:
        yield con


@pytest_asyncio.fixture
async def queue(redis_con):
    return RedisQueue(redis_con)


class TestKeyLayout:
    async def test_a_pushed_job_lives_in_a_hash_and_a_stream(self, queue, redis_con):
        """The stream entry carries the id and nothing else, because a stream entry cannot
        be edited. The stream indexes what is ready; the hash is the job."""

        job_id = await queue.push("send_email", {"to": "a@b.c"})

        fields = await redis_con.hgetall(f"queue:j:{job_id}")
        assert fields["name"] == "send_email"
        assert fields["queue"] == "default"
        assert fields["attempts"] == "0"

        entries = await redis_con.xrange("queue:q:default:s:0")
        assert len(entries) == 1
        assert entries[0][1] == {"job": str(job_id)}

    async def test_each_priority_gets_its_own_stream(self, queue, redis_con):
        """A stream has no ordering but insertion order, so priority cannot be a field on
        an entry - it has to be which stream the entry is in."""

        await queue.push("low", priority=0)
        await queue.push("high", priority=5)

        assert await redis_con.xlen("queue:q:default:s:0") == 1
        assert await redis_con.xlen("queue:q:default:s:5") == 1

        levels = await redis_con.zrevrange("queue:q:default:levels", 0, -1)
        assert levels == ["5", "0"]

    async def test_a_delayed_job_waits_in_a_sorted_set_not_a_stream(self, queue, redis_con):
        await queue.push("later", delay=3600)

        assert await redis_con.zcard("queue:q:default:delayed") == 1
        assert await redis_con.xlen("queue:q:default:s:0") == 0

    async def test_a_custom_prefix_is_honoured_everywhere(self, redis_con):
        queue = RedisQueue(redis_con, prefix="jobs:")
        await queue.push("send_email")

        keys = await redis_con.keys("*")
        assert keys
        assert all(key.startswith("jobs:") for key in keys)


class TestPromotion:
    async def test_a_due_job_is_promoted_out_of_the_delayed_set_on_reserve(self, queue, redis_con):
        """Nothing runs on a timer, so a delayed job only becomes ready when some worker
        reserving that queue promotes it."""

        job_id = await queue.push("later", delay=3600)

        # Bring it forward rather than waiting an hour.
        await redis_con.zadd("queue:q:default:delayed", {str(job_id): 0})

        job = await queue.reserve(["default"], 300, "w1")

        assert job is not None
        assert job.id == job_id
        assert await redis_con.zcard("queue:q:default:delayed") == 0

    async def test_only_the_queue_being_reserved_gets_promoted(self, queue, redis_con):
        other = await queue.push("later", queue="other", delay=3600)
        await redis_con.zadd("queue:q:other:delayed", {str(other): 0})

        assert await queue.reserve(["default"], 300, "w1") is None
        assert await redis_con.zcard("queue:q:other:delayed") == 1


class TestReclaim:
    async def test_a_job_whose_claim_expired_is_handed_back(self, queue, redis_con):
        """XAUTOCLAIM after the visibility timeout is the redis equivalent of postgres's
        reserved_until being in the past: a claim is a deadline, not a flag.

        Both reserves pass the same timeout, and that is not incidental. The min-idle-time
        XAUTOCLAIM is given comes from the worker doing the *reclaiming*, not from whatever
        the original holder asked for - so a fleet whose workers disagreed about the timeout
        would reclaim each other's jobs early or late. The timeout is one config value for
        exactly that reason.
        """

        job_id = await queue.push("send_email")

        held = await queue.reserve(["default"], 1, "dead-worker")
        assert held is not None
        assert held.id == job_id

        # Expire the recorded deadline too, or the parking branch below puts it back rather
        # than handing it over - which is the other half of this mechanism.
        await redis_con.hset(f"queue:j:{job_id}", "reserved_until", "0")
        await asyncio.sleep(1.1)

        again = await queue.reserve(["default"], 1, "w2")

        assert again is not None
        assert again.id == job_id
        # The dead worker's attempt was already spent, so this is the second.
        assert again.attempts == 2

    async def test_a_job_still_legitimately_held_is_parked_rather_than_stolen(self, queue, redis_con):
        """XAUTOCLAIM only knows how long an entry has been idle, not whether the holder is
        alive. reserved_until is the authority, and parking costs no attempt."""

        job_id = await queue.push("send_email")
        held = await queue.reserve(["default"], 3600, "w1")
        assert held is not None

        # Idle long enough for XAUTOCLAIM at a one second timeout, but its recorded claim
        # still has an hour left.
        await asyncio.sleep(1.1)
        stolen = await queue.reserve(["default"], 1, "w2")

        assert stolen is None
        assert int(await redis_con.hget(f"queue:j:{job_id}", "attempts")) == 1
        assert await redis_con.zcard("queue:q:default:delayed") == 1

    async def test_a_tombstone_entry_for_a_finished_job_is_dropped(self, queue, redis_con):
        """A retry that had to wait leaves the stream, so an entry can outlive its hash. It
        must not be handed to a handler as a job with no payload."""

        job_id = await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.delete(job)

        # Put an entry back pointing at the now-deleted hash.
        await redis_con.xadd("queue:q:default:s:0", {"job": str(job_id)})

        assert await queue.reserve(["default"], 300, "w2") is None


class TestConcurrency:
    async def test_two_workers_racing_never_get_the_same_job(self, queue):
        """XREADGROUP delivers each entry to exactly one reader, which is what a list-based
        queue cannot promise and what a fake would happily get wrong."""

        for i in range(20):
            await queue.push(f"job{i}")

        async def drain(worker: str) -> list[int]:
            claimed = []
            while True:
                job = await queue.reserve(["default"], 300, worker)
                if job is None:
                    return claimed
                claimed.append(job.id)

        left, right = await asyncio.gather(drain("w1"), drain("w2"))

        assert sorted(left + right) == sorted(set(left + right))
        assert len(left + right) == 20

    async def test_the_whole_fleet_shares_one_consumer_name(self, queue, redis_con):
        """Naming consumers per host and pid leaks a dead consumer entry on every deploy,
        and idle time is per entry rather than per consumer, so sharing costs nothing."""

        await queue.push("send_email")
        await queue.reserve(["default"], 300, "some-host:123")

        consumers = await redis_con.xinfo_consumers("queue:q:default:s:0", "workers")
        assert [c["name"] for c in consumers] == [CONSUMER]


class TestFailedJobs:
    async def test_a_failed_job_keeps_its_id(self, queue, redis_con):
        """Unlike the database driver, where the failed table gives the row a new id. Here
        the hash never moves, so `queue retry` puts back the job that failed rather than a
        reconstruction of it."""

        job_id = await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        rows = await queue.failed_rows(10)
        assert rows[0]["id"] == job_id

    async def test_retrying_a_live_pending_job_by_id_does_nothing(self, queue, redis_con):
        """Without the early return, naming a live job would reset it and add a second stream
        entry for it - two deliveries of one job."""

        job_id = await queue.push("send_email")

        assert await queue.retry_failed(job_id, 3) == 0
        assert await redis_con.xlen("queue:q:default:s:0") == 1

    async def test_a_retried_job_goes_back_at_priority_zero(self, queue, redis_con):
        job_id = await queue.push("send_email", priority=5)
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.retry_failed(None, 3) == 1
        assert await redis_con.xlen("queue:q:default:s:0") == 1
        assert await redis_con.hget(f"queue:j:{job_id}", "priority") == "0"

    async def test_forgetting_a_failed_job_removes_its_hash(self, queue, redis_con):
        job_id = await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.forget_failed(job_id, None) == 1
        assert await redis_con.exists(f"queue:j:{job_id}") == 0


class TestUniqueKeys:
    async def test_a_stale_unique_key_pointing_at_a_gone_job_is_reclaimed(self, queue, redis_con):
        """Nothing expires these keys, so a job removed by hand would otherwise block its
        key forever."""

        job_id = await queue.push("rebuild", unique="rebuild:7")
        await redis_con.delete(f"queue:j:{job_id}")

        fresh = await queue.push("rebuild", unique="rebuild:7")

        assert fresh != job_id


class TestScriptReloading:
    async def test_the_scripts_survive_the_server_forgetting_them(self, queue, redis_con):
        """A SCRIPT FLUSH, or a restart, otherwise turns every call into a NOSCRIPT error
        the caller has to know to retry."""

        await queue.push("send_email")
        await redis_con.script_flush()

        assert await queue.push("send_email") > 0
        assert await queue.pending() == 2
