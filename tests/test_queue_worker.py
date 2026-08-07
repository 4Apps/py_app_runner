import asyncio
import pathlib

import psycopg
import pytest
import pytest_asyncio

from py_app_runner.queue.driver_pg import PgQueue
from py_app_runner.queue.driver_redis import RedisQueue
from py_app_runner.queue.handler import assert_resolvable, resolve
from py_app_runner.queue.job import Job, QueueError
from py_app_runner.queue.worker import Worker, backoff
from tests.migrations_pg import pg_dsn_for
from tests.redis_harness import redis_for

SCHEMA = pathlib.Path(__file__).parent.parent / "src" / "py_app_runner" / "queue" / "files" / "install.pgsql.sql"

# Module-level so the handler resolver can import them by path, the way a real one would.
ran: list[dict] = []


async def records(payload: dict, job: Job) -> None:
    ran.append(payload)


async def always_fails(payload: dict, job: Job) -> None:
    raise RuntimeError("nope")


async def releases_itself(payload: dict, job: Job) -> None:
    job.release(30)


async def never_returns(payload: dict, job: Job) -> None:
    await asyncio.sleep(30)


class RecordingHandler:
    async def handle(self, payload: dict, job: Job) -> None:
        ran.append(payload)


@pytest_asyncio.fixture
async def conn():
    async with pg_dsn_for("par_test_queue_worker") as dsn:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            async with connection.cursor() as cur:
                await cur.execute(SCHEMA.read_text(encoding="utf-8"))
            yield connection


@pytest_asyncio.fixture
async def queue(conn):
    ran.clear()
    return PgQueue(conn)


def worker(queue, **kwargs) -> Worker:
    lines: list[str] = []
    return Worker(queue=queue, out=lines.append, **kwargs)


class TestHandlerResolution:
    def test_resolves_a_module_path(self):
        assert resolve("tests.test_queue_worker:records") is records

    def test_resolves_a_dotted_path(self):
        assert resolve("tests.test_queue_worker.records") is records

    def test_resolves_a_class_to_its_handle_method(self):
        handler = resolve("tests.test_queue_worker:RecordingHandler")
        assert handler.__self__.__class__ is RecordingHandler

    def test_a_configured_alias_wins_and_short_circuits(self):
        """What lets a renamed class keep working without touching the rows that name it."""

        handler = resolve("send-email", {"send-email": "tests.test_queue_worker:records"})
        assert handler is records

    def test_an_unknown_name_is_refused_at_push_time(self):
        """A name that resolves to nothing fails on every attempt and then sits in the failed
        table, having spent its whole budget discovering something knowable at the call
        site."""

        with pytest.raises(QueueError) as excinfo:
            assert_resolvable("tests.test_queue_worker:no_such_thing")
        assert "no_such_thing" in str(excinfo.value)

    def test_an_empty_name_is_refused(self):
        with pytest.raises(QueueError):
            assert_resolvable("")


class TestBackoff:
    def test_walks_the_steps_then_repeats_the_last(self):
        assert backoff(1) == 10
        assert backoff(2) == 60
        assert backoff(3) == 300
        assert backoff(9) == 300

    def test_a_scalar_is_a_constant_delay(self):
        assert backoff(5, 30) == 30

    def test_an_empty_list_means_no_delay(self):
        assert backoff(3, []) == 0


class TestRunningJobs:
    async def test_runs_a_job_and_deletes_it(self, queue):
        await queue.push("tests.test_queue_worker:records", {"n": 1})

        assert await worker(queue).run_next(["default"]) is True
        assert ran == [{"n": 1}]
        assert await queue.pending() == 0

    async def test_an_empty_queue_runs_nothing(self, queue):
        assert await worker(queue).run_next(["default"]) is False

    async def test_a_failing_job_is_released_with_backoff(self, queue):
        await queue.push("tests.test_queue_worker:always_fails", max_attempts=3)

        await worker(queue).run_next(["default"])

        # Released with a delay, so not immediately pending, but still on the queue.
        assert await queue.pending() == 0
        assert await queue.failed_count() == 0
        async with queue.conn.cursor() as cur:
            await cur.execute("SELECT attempts, last_error FROM queue_jobs")
            attempts, last_error = await cur.fetchone()
        assert attempts == 1
        assert "nope" in last_error

    async def test_a_job_out_of_attempts_lands_in_the_failed_table(self, queue):
        await queue.push("tests.test_queue_worker:always_fails", max_attempts=1)

        await worker(queue).run_next(["default"])

        assert await queue.failed_count() == 1
        rows = await queue.failed_rows(10)
        assert "nope" in rows[0]["error"]

    async def test_a_handler_that_cannot_be_resolved_fails_the_job_like_any_other(self, queue):
        """Resolution is inside the same try as the call, so "could not start" is not a
        separate outcome nobody handles."""

        await queue.push("tests.test_queue_worker:gone", max_attempts=1)

        await worker(queue).run_next(["default"])

        assert await queue.failed_count() == 1

    async def test_a_job_can_release_itself_without_being_a_failure(self, queue):
        """A deliberate "not now" - a rate limit upstream, a file that has not landed."""

        await queue.push("tests.test_queue_worker:releases_itself", max_attempts=1)

        await worker(queue).run_next(["default"])

        assert await queue.failed_count() == 0
        async with queue.conn.cursor() as cur:
            await cur.execute("SELECT last_error FROM queue_jobs")
            assert (await cur.fetchone())[0] == ""

    async def test_a_job_that_overruns_its_timeout_is_cancelled(self, queue):
        """asyncio.timeout genuinely cancels the task. A signal-based timeout cannot
        interrupt a job blocked in a query, which is the case most worth interrupting."""

        await queue.push("tests.test_queue_worker:never_returns", max_attempts=1)

        await asyncio.wait_for(worker(queue).run_next(["default"], timeout=1), timeout=10)

        assert await queue.failed_count() == 1


class TestRunLoop:
    async def test_stop_when_empty_exits_cleanly(self, queue):
        await queue.push("tests.test_queue_worker:records", {"n": 1})

        code = await worker(queue).run(["default"], sleep=0.01, stop_when_empty=True)

        assert code == 0
        assert ran == [{"n": 1}]

    async def test_max_jobs_stops_after_the_count(self, queue):
        for i in range(5):
            await queue.push("tests.test_queue_worker:records", {"n": i})

        code = await worker(queue).run(["default"], sleep=0.01, max_jobs=2)

        assert code == 0
        assert len(ran) == 2
        assert await queue.pending() == 3

    async def test_a_failed_job_does_not_change_the_exit_code(self, queue):
        """Supervisors read the exit code. A job that failed is what the failed table is
        for; the worker did its job correctly."""

        await queue.push("tests.test_queue_worker:always_fails", max_attempts=1)

        code = await worker(queue).run(["default"], sleep=0.01, stop_when_empty=True)

        assert code == 0
        assert await queue.failed_count() == 1

    async def test_stop_asks_the_loop_to_finish(self, queue):
        w = worker(queue)
        w.stop()

        code = await w.run(["default"], sleep=0.01)

        assert code == 0

    async def test_an_unreadable_queue_eventually_exits_one(self):
        """Five consecutive failures means the queue is unreadable, not empty. Exiting lets
        a supervisor restart the process rather than having it sit there logging."""

        class Broken:
            async def reserve(self, *args, **kwargs):
                raise RuntimeError("connection is gone")

        lines: list[str] = []
        code = await Worker(queue=Broken(), out=lines.append).run(["default"], sleep=0.01)

        assert code == 1
        assert any("unreadable" in line for line in lines)


class TestWorkerAgainstRedis:
    """The worker is written against the interface, so it must run on either driver without
    knowing which one it has. This is the test that proves the abstraction is real rather
    than just declared."""

    @pytest_asyncio.fixture
    async def redis_queue(self):
        ran.clear()
        async with redis_for(database=6) as con:
            yield RedisQueue(con)

    async def test_runs_a_job_and_deletes_it(self, redis_queue):
        await redis_queue.push("tests.test_queue_worker:records", {"n": 1})

        assert await worker(redis_queue).run_next(["default"]) is True
        assert ran == [{"n": 1}]
        assert await redis_queue.pending() == 0

    async def test_a_job_out_of_attempts_lands_in_the_failed_list(self, redis_queue):
        await redis_queue.push("tests.test_queue_worker:always_fails", max_attempts=1)

        await worker(redis_queue).run_next(["default"])

        assert await redis_queue.failed_count() == 1
        rows = await redis_queue.failed_rows(10)
        assert "nope" in rows[0]["error"]

    async def test_a_failing_job_is_released_with_backoff(self, redis_queue):
        await redis_queue.push("tests.test_queue_worker:always_fails", max_attempts=3)

        await worker(redis_queue).run_next(["default"])

        assert await redis_queue.failed_count() == 0
        # Released with a delay, so not due yet.
        assert await redis_queue.pending() == 0

    async def test_the_run_loop_drains_and_exits_cleanly(self, redis_queue):
        for i in range(3):
            await redis_queue.push("tests.test_queue_worker:records", {"n": i})

        code = await worker(redis_queue).run(["default"], sleep=0.01, stop_when_empty=True)

        assert code == 0
        assert len(ran) == 3
