"""The behaviour both drivers owe callers, run against each of them.

A worker, a handler and every CLI command are written against the interface, not against a
driver, so anything they rely on has to hold on both. Keeping that in one parametrised suite
is what stops the two drifting apart - the alternative is two test files that gradually stop
describing the same queue.

Driver-specific mechanics live in test_queue_driver.py (postgres) and
test_queue_driver_redis.py (redis). What is deliberately *not* here:

- Transactional push. It is the whole point of the database driver and redis cannot do it.
- Id identity across a failure. The database driver's failed table gives a row a new id;
  redis keeps the same one, because the hash never moves.
- Ordering at equal priority. Postgres orders by available_at then id; redis by stream
  insertion order, so a released job rejoins at the back rather than in due order.
"""

import datetime
import pathlib

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.queue.driver_pg import PgQueue
from py_app_runner.queue.driver_redis import RedisQueue
from py_app_runner.queue.job import QueueError
from tests.migrations_pg import pg_dsn_for
from tests.redis_harness import redis_for

SCHEMA = pathlib.Path(__file__).parent.parent / "src" / "py_app_runner" / "queue" / "files" / "install.pgsql.sql"


@pytest_asyncio.fixture(params=["postgres", "redis"])
async def queue(request):
    """One queue, either driver. Every test below runs twice."""

    if request.param == "postgres":
        async with pg_dsn_for("par_test_queue_contract") as dsn:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(SCHEMA.read_text(encoding="utf-8"))
                yield PgQueue(conn)
        return

    async with redis_for(database=4) as con:
        yield RedisQueue(con)


class TestPush:
    async def test_returns_an_id(self, queue):
        assert await queue.push("send_email", {"to": "a@b.c"}) > 0

    async def test_a_pushed_job_is_immediately_pending(self, queue):
        await queue.push("send_email")
        assert await queue.pending() == 1

    async def test_a_delayed_job_is_not_pending_yet(self, queue):
        await queue.push("send_email", delay=3600)
        assert await queue.pending() == 0

    async def test_a_payload_that_will_not_encode_is_refused(self, queue):
        with pytest.raises(QueueError):
            await queue.push("send_email", {"when": object()})

    async def test_a_unique_key_returns_the_existing_job_rather_than_queueing_twice(self, queue):
        first = await queue.push("rebuild", unique="rebuild:7")
        second = await queue.push("rebuild", unique="rebuild:7")

        assert first == second
        assert await queue.pending() == 1

    async def test_jobs_without_a_unique_key_do_not_collide(self, queue):
        await queue.push("a")
        await queue.push("b")

        assert await queue.pending() == 2


class TestReserve:
    async def test_reserving_is_the_attempt(self, queue):
        """attempts increments on claim, not on failure, so the first handle() sees 1."""

        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")

        assert job is not None
        assert job.attempts == 1

    async def test_an_empty_queue_reserves_nothing(self, queue):
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_the_payload_arrives_intact(self, queue):
        await queue.push("send_email", {"to": "a@b.c", "n": 3, "nested": {"x": [1, 2]}})
        job = await queue.reserve(["default"], 300, "w1")

        assert job.payload == {"to": "a@b.c", "n": 3, "nested": {"x": [1, 2]}}

    async def test_the_name_is_what_was_pushed(self, queue):
        """Not the resolved target: rows outlive the deploy that wrote them."""

        await queue.push("send-email-alias")
        job = await queue.reserve(["default"], 300, "w1")

        assert job.name == "send-email-alias"

    async def test_a_delayed_job_is_not_reserved_before_it_is_due(self, queue):
        await queue.push("send_email", delay=3600)
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_higher_priority_goes_first(self, queue):
        await queue.push("low", priority=0)
        await queue.push("high", priority=10)

        job = await queue.reserve(["default"], 300, "w1")
        assert job.name == "high"

    async def test_queues_are_a_precedence_order_not_a_merged_sort(self, queue):
        await queue.push("d1", queue="default")
        await queue.push("h1", queue="high")

        first = await queue.reserve(["high", "default"], 300, "w1")
        assert first.name == "h1"

    async def test_a_reserved_job_is_not_handed_to_a_second_worker(self, queue):
        await queue.push("send_email")

        first = await queue.reserve(["default"], 300, "w1")
        second = await queue.reserve(["default"], 300, "w2")

        assert first is not None
        assert second is None

    async def test_a_reserved_job_is_not_counted_as_pending(self, queue):
        await queue.push("send_email")
        await queue.reserve(["default"], 300, "w1")

        assert await queue.pending() == 0

    async def test_every_job_is_handed_out_exactly_once(self, queue):
        """Sequential rather than concurrent. Genuine concurrency needs a connection per
        worker on postgres, which one shared driver instance cannot express, so the racing
        case is tested per driver - see test_queue_driver.py and
        test_queue_driver_redis.py."""

        for i in range(10):
            await queue.push(f"job{i}")

        claimed = []
        while True:
            job = await queue.reserve(["default"], 300, "w1")
            if job is None:
                break
            claimed.append(job.id)
            await queue.delete(job)

        assert len(claimed) == 10
        assert len(set(claimed)) == 10


class TestCompletion:
    async def test_delete_removes_the_job(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.delete(job)

        assert await queue.pending() == 0
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_release_puts_it_back_without_spending_another_attempt(self, queue):
        """The claim already counted it, so a job that releases once must not burn two of
        its three tries."""

        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.release(job, 0, "not yet")

        again = await queue.reserve(["default"], 300, "w1")
        assert again.attempts == 2

    async def test_release_with_a_delay_holds_the_job_back(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.release(job, 3600, "later")

        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_fail_takes_it_off_the_queue_and_into_the_failed_list(self, queue):
        await queue.push("send_email", {"to": "a@b.c"})
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "it broke")

        assert await queue.failed_count() == 1
        assert await queue.pending() == 0
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_a_unique_key_is_released_when_the_job_completes(self, queue):
        """Uniqueness is scoped to pending, so the same work can be queued again once the
        first one is done."""

        await queue.push("rebuild", unique="rebuild:7")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.delete(job)

        assert await queue.push("rebuild", unique="rebuild:7") > 0
        assert await queue.pending() == 1

    async def test_a_unique_key_is_released_when_the_job_fails(self, queue):
        await queue.push("rebuild", unique="rebuild:7")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.push("rebuild", unique="rebuild:7") > 0
        assert await queue.pending() == 1


class TestReports:
    async def test_stats_splits_pending_delayed_and_held(self, queue):
        await queue.push("a")
        await queue.push("b", delay=3600)
        await queue.push("c")
        await queue.reserve(["default"], 300, "w1")

        rows = await queue.stats()
        assert len(rows) == 1
        assert rows[0]["queue"] == "default"
        assert rows[0]["pending"] == 1
        assert rows[0]["delayed"] == 1
        assert rows[0]["reserved"] == 1
        assert rows[0]["total"] == 3

    async def test_stats_is_empty_on_an_empty_queue(self, queue):
        assert await queue.stats() == []

    async def test_pending_can_be_narrowed_to_one_queue(self, queue):
        await queue.push("a", queue="default")
        await queue.push("b", queue="high")

        assert await queue.pending("default") == 1
        assert await queue.pending() == 2

    async def test_failed_rows_carry_what_the_cli_prints(self, queue):
        await queue.push("send_email", {"to": "a@b.c"})
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "it broke")

        rows = await queue.failed_rows(10)
        assert len(rows) == 1
        assert rows[0]["queue"] == "default"
        assert rows[0]["name"] == "send_email"
        assert rows[0]["attempts"] == 1
        assert "it broke" in rows[0]["error"]
        assert rows[0]["failed_at"]

    async def test_retry_puts_a_failed_job_back_with_a_fresh_budget(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.retry_failed(None, 3) == 1
        assert await queue.failed_count() == 0

        again = await queue.reserve(["default"], 300, "w1")
        assert again is not None
        assert again.attempts == 1

    async def test_retry_by_id_moves_only_that_one(self, queue):
        ids = []
        for _ in range(2):
            await queue.push("send_email")
            job = await queue.reserve(["default"], 300, "w1")
            await queue.fail(job, "broke")
            ids.append(job.id)

        rows = await queue.failed_rows(10)
        assert await queue.retry_failed(rows[0]["id"], 3) == 1
        assert await queue.failed_count() == 1

    async def test_forget_deletes_without_requeueing(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.forget_failed(None, None) == 1
        assert await queue.failed_count() == 0
        assert await queue.pending() == 0

    async def test_forget_by_date_keeps_newer_failures(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        long_ago = "2020-01-01"
        assert await queue.forget_failed(None, long_ago) == 0
        assert await queue.failed_count() == 1

        tomorrow = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        assert await queue.forget_failed(None, tomorrow) == 1
        assert await queue.failed_count() == 0
