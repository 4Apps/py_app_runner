import asyncio
import datetime
import pathlib

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.queue.driver_pg import PgQueue, fit_error
from py_app_runner.queue.job import QueueError
from tests.migrations_pg import pg_dsn_for

SCHEMA = pathlib.Path(__file__).parent.parent / "src" / "py_app_runner" / "queue" / "files" / "install.pgsql.sql"


@pytest_asyncio.fixture
async def db_dsn():
    async with pg_dsn_for("par_test_queue") as dsn:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            async with connection.cursor() as cur:
                await cur.execute(SCHEMA.read_text(encoding="utf-8"))
        yield dsn


@pytest_asyncio.fixture
async def conn(db_dsn):
    async with await psycopg.AsyncConnection.connect(db_dsn, autocommit=True) as connection:
        yield connection


@pytest_asyncio.fixture
async def queue(conn):
    return PgQueue(conn)


class TestPush:
    async def test_returns_the_new_id(self, queue):
        assert await queue.push("send_email", {"to": "a@b.c"}) > 0

    async def test_a_pushed_job_is_immediately_pending(self, queue):
        await queue.push("send_email")
        assert await queue.pending() == 1

    async def test_a_delayed_job_is_not_pending_yet(self, queue):
        await queue.push("send_email", delay=3600)
        assert await queue.pending() == 0

    async def test_a_payload_that_will_not_encode_is_refused_at_push(self, queue):
        with pytest.raises(QueueError) as excinfo:
            await queue.push("send_email", {"when": object()})
        assert "JSON encodable" in str(excinfo.value)

    async def test_a_unique_key_returns_the_existing_job_rather_than_queueing_twice(self, queue):
        first = await queue.push("rebuild", unique="rebuild:7")
        second = await queue.push("rebuild", unique="rebuild:7")

        assert first == second
        assert await queue.pending() == 1

    async def test_jobs_without_a_unique_key_do_not_collide(self, queue):
        """The unique index relies on repeated NULLs being allowed, which is what lets one
        index cover both uniqueness and "most jobs have no key"."""

        await queue.push("a")
        await queue.push("b")

        assert await queue.pending() == 2


class TestPushJoinsTheCallersTransaction:
    async def test_a_rolled_back_push_queues_nothing(self, db_dsn, queue):
        """The entire argument for keeping jobs in the application's own database. Queue the
        confirmation inside the transaction that writes the payment and either both happen
        or neither does."""

        async with await psycopg.AsyncConnection.connect(db_dsn) as tx_conn:
            await PgQueue(tx_conn).push("send_email")
            await tx_conn.rollback()

        assert await queue.pending() == 0

    async def test_a_committed_push_queues_the_job(self, db_dsn, queue):
        async with await psycopg.AsyncConnection.connect(db_dsn) as tx_conn:
            await PgQueue(tx_conn).push("send_email")
            await tx_conn.commit()

        assert await queue.pending() == 1


class TestReserve:
    async def test_reserving_is_the_attempt(self, queue):
        """attempts increments on claim, not on failure, so the first handle() sees 1."""

        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")

        assert job is not None
        assert job.attempts == 1

    async def test_an_empty_queue_reserves_nothing(self, queue):
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_a_delayed_job_is_not_reserved_before_it_is_due(self, queue):
        await queue.push("send_email", delay=3600)
        assert await queue.reserve(["default"], 300, "w1") is None

    async def test_higher_priority_goes_first(self, queue):
        await queue.push("low", priority=0)
        await queue.push("high", priority=10)

        job = await queue.reserve(["default"], 300, "w1")
        assert job.name == "high"

    async def test_queues_are_a_precedence_order_not_a_merged_sort(self, queue):
        """["high", "default"] drains high completely first. A merged sort would quietly
        make "high" mean "slightly sooner"."""

        await queue.push("d1", queue="default")
        await queue.push("d2", queue="default")
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

    async def test_two_workers_racing_never_get_the_same_job(self, db_dsn, queue):
        """The guard in the UPDATE's WHERE - not SKIP LOCKED - is what makes this true.
        SKIP LOCKED only stops the workers queueing behind each other."""

        for i in range(10):
            await queue.push(f"job{i}")

        async def drain(worker: str) -> list[int]:
            claimed = []
            async with await psycopg.AsyncConnection.connect(db_dsn, autocommit=True) as con:
                q = PgQueue(con)
                while True:
                    job = await q.reserve(["default"], 300, worker)
                    if job is None:
                        return claimed
                    claimed.append(job.id)

        left, right = await asyncio.gather(drain("w1"), drain("w2"))

        assert sorted(left + right) == sorted(set(left + right))
        assert len(left + right) == 10


class TestClaimIsADeadline:
    async def test_an_expired_reservation_becomes_claimable_again(self, queue, conn):
        """A worker killed mid-job leaves the row claimable once the deadline passes, with
        its attempt already spent. No heartbeat, no lease renewal, no unlock path."""

        await queue.push("send_email")
        first = await queue.reserve(["default"], 300, "dead-worker")

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE queue_jobs SET reserved_until = %s WHERE id = %s",
                (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1), first.id),
            )

        again = await queue.reserve(["default"], 300, "w2")

        assert again is not None
        assert again.id == first.id
        assert again.attempts == 2


class TestCompletion:
    async def test_delete_removes_the_job(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.delete(job)

        assert await queue.pending() == 0
        async with queue.conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM queue_jobs")
            assert (await cur.fetchone())[0] == 0

    async def test_release_puts_it_back_without_spending_another_attempt(self, queue):
        """The claim already counted it, so releasing must not count it again - otherwise a
        job that releases once burns two of its three tries."""

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

    async def test_fail_moves_it_to_the_failed_table(self, queue):
        await queue.push("send_email", {"to": "a@b.c"})
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "it broke")

        assert await queue.failed_count() == 1
        async with queue.conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM queue_jobs")
            assert (await cur.fetchone())[0] == 0

    async def test_a_failed_job_keeps_its_payload_byte_for_byte(self, queue):
        await queue.push("send_email", {"to": "a@b.c"})
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "it broke")

        rows = await queue.failed_rows(10)
        async with queue.conn.cursor() as cur:
            await cur.execute("SELECT payload FROM queue_failed_jobs WHERE id = %s", (rows[0]["id"],))
            assert (await cur.fetchone())[0] == job.payload_json

    async def test_a_unique_key_is_released_when_the_job_completes(self, queue):
        """Uniqueness is scoped to pending, so the same work can legitimately be queued
        again once the first one is done."""

        await queue.push("rebuild", unique="rebuild:7")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.delete(job)

        assert await queue.push("rebuild", unique="rebuild:7") > 0
        assert await queue.pending() == 1


class TestBadPayload:
    async def test_an_undecodable_payload_goes_straight_to_failed(self, queue, conn):
        """It is not going to decode on the next attempt either, so retrying it three times
        only delays the moment somebody notices."""

        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO queue_jobs (queue, name, payload, available_at, created_at) "
                "VALUES ('default', 'broken', 'not json', now(), now())"
            )

        assert await queue.reserve(["default"], 300, "w1") is None
        assert await queue.failed_count() == 1


class TestReports:
    async def test_stats_splits_pending_delayed_and_held(self, queue):
        await queue.push("a")
        await queue.push("b", delay=3600)
        await queue.push("c")
        await queue.reserve(["default"], 300, "w1")

        rows = await queue.stats()
        assert len(rows) == 1
        assert rows[0]["pending"] == 1
        assert rows[0]["delayed"] == 1
        assert rows[0]["reserved"] == 1
        assert rows[0]["total"] == 3

    async def test_retry_puts_a_failed_job_back_with_a_fresh_budget(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.retry_failed(None, 3) == 1
        assert await queue.failed_count() == 0

        again = await queue.reserve(["default"], 300, "w1")
        assert again.attempts == 1

    async def test_forget_deletes_without_requeueing(self, queue):
        await queue.push("send_email")
        job = await queue.reserve(["default"], 300, "w1")
        await queue.fail(job, "broke")

        assert await queue.forget_failed(None, None) == 1
        assert await queue.failed_count() == 0
        assert await queue.pending() == 0


class TestErrorTruncation:
    def test_a_huge_error_is_cut_rather_than_refused(self):
        """A traceback that will not fit must not be the reason a job cannot be recorded as
        failed."""

        cut = fit_error("x" * 100000)
        assert cut.endswith("... truncated")
        assert len(cut.encode("utf-8")) < 100000

    def test_cutting_never_produces_invalid_utf8(self):
        """Slicing bytes can split a multi-byte sequence, and Postgres refuses the result -
        which would turn a failed job into a failed write."""

        cut = fit_error("ā" * 50000)
        cut.encode("utf-8").decode("utf-8")

    def test_a_short_error_is_left_alone(self):
        assert fit_error("small") == "small"


class TestTableGuard:
    async def test_refuses_a_table_name_that_is_not_a_plain_identifier(self, conn):
        with pytest.raises(QueueError):
            PgQueue(conn, table="queue_jobs; DROP TABLE queue_jobs")
