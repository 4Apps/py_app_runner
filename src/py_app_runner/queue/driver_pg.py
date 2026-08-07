"""The PostgreSQL driver.

This is the one to reach for first, and the reason is the push: it is an INSERT, so it
joins the transaction that caused it. Queue the confirmation email inside the transaction
that writes the payment and either both happen or neither does. No other backend can offer
that, and it is the entire argument for keeping jobs in the application's own database
rather than somewhere faster.
"""

import datetime
import json
import re
from typing import Any

import psycopg
from psycopg import sql

from py_app_runner.queue.job import Job, QueueError

# Table names reach SQL as identifiers, which cannot be bound. Quoted by Identifier, but
# whitelisted first for the same reason the audit table is.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# A claim can be lost to another worker between the candidate select and the guarded
# update. Retrying a few times is cheaper than sleeping the whole poll interval when there
# is obviously work; giving up after five stops a hot queue from spinning here forever.
CLAIM_ROUNDS = 5

# Errors are cut rather than rejected. A traceback that will not fit must not be the reason
# a job cannot be recorded as failed.
MAX_ERROR_BYTES = 60000

_JOB_COLUMNS = "id, queue, name, payload, attempts, max_attempts, priority, unique_key"


def assert_table_name(table: str) -> str:
    if _TABLE_RE.match(table) is None:
        raise QueueError(f"{table!r} is not a plain table name")

    return table


def _qualified(table: str) -> sql.Composed:
    return sql.SQL(".").join(sql.Identifier(part) for part in table.split("."))


def fit_error(error: str) -> str:
    raw = error.encode("utf-8")
    if len(raw) <= MAX_ERROR_BYTES:
        return error

    # Cut on a character boundary: slicing bytes can split a multi-byte sequence and leave
    # a value Postgres refuses as invalid UTF-8, which would turn a failed job into a
    # failed *write*.
    return raw[:MAX_ERROR_BYTES].decode("utf-8", errors="ignore") + "\n... truncated"


def encode_payload(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise QueueError(f"A job payload has to be JSON encodable: {e}") from e


def column_names(cur: psycopg.AsyncCursor) -> list[str]:
    """Column names of the last result set.

    `description` is None for a statement that returned no result set at all. Every caller
    here has just run a SELECT, so that is an invariant rather than a case - but reading it
    unguarded is how a refactor that moves one of these onto a non-returning statement gets
    a `NoneType is not iterable` a long way from the cause.
    """

    if cur.description is None:
        raise QueueError("Expected a result set from the queue, and the statement returned none.")

    return [d.name for d in cur.description]


def now_utc() -> datetime.datetime:
    """Every time comparison binds a value computed here rather than using now().

    The worker's clock and the database's are allowed to differ; what must not happen is
    the two being mixed inside one comparison, which is what makes a job's due time depend
    on which machine asked.
    """

    return datetime.datetime.now(datetime.UTC)


class PgQueue:
    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        table: str = "queue_jobs",
        failed_table: str = "queue_failed_jobs",
    ) -> None:
        self.conn = conn
        self.table = assert_table_name(table)
        self.failed_table = assert_table_name(failed_table)

    ############
    ### Push ###
    ############

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
        """Queue a job. Returns its id, or the id of the job already holding `unique`.

        Deliberately not wrapped in a transaction of its own: the whole point is that it
        joins whatever the caller already has open.
        """

        moment = now_utc()
        key = unique or None

        if key is not None:
            existing = await self._id_for_unique(key)
            if existing is not None:
                return existing

        row = {
            "queue": queue or "default",
            "name": name,
            "payload": encode_payload(payload or {}),
            "attempts": 0,
            "max_attempts": max(1, max_attempts),
            "priority": priority,
            "unique_key": key,
            "available_at": moment + datetime.timedelta(seconds=max(0, delay)),
            "last_error": "",
            "created_at": moment,
        }

        columns = list(row.keys())
        statement = sql.SQL("INSERT INTO {rel} ({cols}) VALUES ({vals}) RETURNING id").format(
            rel=_qualified(self.table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )

        try:
            async with self.conn.cursor() as cur:
                await cur.execute(statement, tuple(row[c] for c in columns))
                returned = await cur.fetchone()
                if returned is None:
                    # RETURNING on a successful single-row INSERT always yields a row, so
                    # this is an invariant rather than a case - but silently returning 0
                    # would hand the caller a job id that refers to nothing.
                    raise QueueError("The queue insert reported no id; the job may not have been queued.")

                return int(returned[0])
        except psycopg.errors.UniqueViolation:
            # Lost the race for the unique key. On Postgres this aborts the surrounding
            # transaction, so the caller has to decide what to do next - but reporting the
            # winner's id is still the honest answer to "what is queued under this key".
            existing = await self._id_for_unique(key) if key is not None else None
            if existing is not None:
                return existing

            raise

    async def _id_for_unique(self, key: str) -> int | None:
        statement = sql.SQL("SELECT id FROM {rel} WHERE unique_key = %s").format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (key,))
            row = await cur.fetchone()
            return int(row[0]) if row else None

    ###############
    ### Reserve ###
    ###############

    async def reserve(self, queues: list[str], timeout: int, worker: str) -> Job | None:
        """Claim the next due job, or None.

        `queues` is a precedence order, not a merged sort: ["high", "default"] drains high
        completely first. That is what "priority queue" means to somebody running two of
        them, and a merged sort would quietly make `high` mean "slightly sooner".
        """

        for queue in queues:
            job = await self._reserve_from(queue, timeout, worker)
            if job is not None:
                return job

        return None

    async def _reserve_from(self, queue: str, timeout: int, worker: str) -> Job | None:
        for _ in range(CLAIM_ROUNDS):
            moment = now_utc()
            until = moment + datetime.timedelta(seconds=max(1, timeout))

            # One transaction around candidate-and-claim, so the FOR UPDATE lock still holds
            # when the UPDATE runs. It ends before the handler is called: holding a
            # transaction open for the length of a job is how a queue takes a database down
            # with it.
            async with self.conn.transaction():
                candidate = await self._candidate(queue, moment)
                if candidate is None:
                    return None

                claimed = await self._claim(candidate, until, worker, moment)
                if not claimed:
                    # Another worker got it between the select and the update. The guard did
                    # its job; try for a different one.
                    continue

                row = await self._read(candidate)

            if row is None:
                continue

            job = self._to_job(row)
            if job is None:
                # Undecodable payload. It is not going to decode on the next attempt either,
                # so it goes straight to failed rather than burning its budget first.
                await self._fail_row(row, "Payload is not valid JSON, so no handler could be given it.")
                continue

            return job

        return None

    async def _candidate(self, queue: str, moment: datetime.datetime) -> int | None:
        statement = sql.SQL(
            "SELECT id FROM {rel} "
            "WHERE queue = %s AND available_at <= %s "
            "  AND (reserved_until IS NULL OR reserved_until <= %s) "
            "ORDER BY priority DESC, available_at, id "
            "LIMIT 1 FOR UPDATE SKIP LOCKED"
        ).format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (queue, moment, moment))
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def _claim(self, job_id: int, until: datetime.datetime, worker: str, moment: datetime.datetime) -> bool:
        """The guarded update.

        The guard in the WHERE - not SKIP LOCKED - is what makes the claim safe. SKIP LOCKED
        only stops workers queueing behind each other; remove the guard and two workers that
        both read the same candidate would both believe they hold it. This is exactly the
        line a later simplification would delete, so it is spelled out here.
        """

        statement = sql.SQL(
            "UPDATE {rel} SET reserved_until = %s, reserved_by = %s, attempts = attempts + 1 "
            "WHERE id = %s AND (reserved_until IS NULL OR reserved_until <= %s)"
        ).format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (until, worker[:64], job_id, moment))
            return cur.rowcount == 1

    async def _read(self, job_id: int) -> dict[str, Any] | None:
        statement = sql.SQL("SELECT " + _JOB_COLUMNS + " FROM {rel} WHERE id = %s").format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (job_id,))
            row = await cur.fetchone()
            if row is None:
                return None

            return dict(zip(column_names(cur), row, strict=True))

    def _to_job(self, row: dict[str, Any]) -> Job | None:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

        if not isinstance(payload, dict):
            return None

        return Job(
            id=int(row["id"]),
            queue=row["queue"],
            name=row["name"],
            payload=payload,
            payload_json=row["payload"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )

    ##################
    ### Completion ###
    ##################

    async def delete(self, job: Job) -> None:
        statement = sql.SQL("DELETE FROM {rel} WHERE id = %s").format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (job.id,))

    async def release(self, job: Job, delay: int = 0, error: str = "") -> None:
        """Put a job back. `attempts` is untouched - the claim already counted it."""

        statement = sql.SQL(
            "UPDATE {rel} SET reserved_until = NULL, reserved_by = NULL, available_at = %s, last_error = %s "
            "WHERE id = %s"
        ).format(rel=_qualified(self.table))

        available = now_utc() + datetime.timedelta(seconds=max(0, delay))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (available, fit_error(error), job.id))

    async def fail(self, job: Job, error: str) -> None:
        await self._move_to_failed(
            job_id=job.id,
            queue=job.queue,
            name=job.name,
            payload=job.payload_json,
            attempts=job.attempts,
            error=error,
        )

    async def _fail_row(self, row: dict[str, Any], error: str) -> None:
        await self._move_to_failed(
            job_id=int(row["id"]),
            queue=row["queue"],
            name=row["name"],
            payload=row["payload"],
            attempts=int(row["attempts"]),
            error=error,
        )

    async def _move_to_failed(
        self, job_id: int, queue: str, name: str, payload: str, attempts: int, error: str
    ) -> None:
        insert = sql.SQL(
            "INSERT INTO {rel} (queue, name, payload, attempts, error, failed_at) VALUES (%s, %s, %s, %s, %s, %s)"
        ).format(rel=_qualified(self.failed_table))
        delete = sql.SQL("DELETE FROM {rel} WHERE id = %s").format(rel=_qualified(self.table))

        # One transaction so a job cannot be in both tables or neither.
        async with self.conn.transaction():
            async with self.conn.cursor() as cur:
                await cur.execute(insert, (queue, name, payload, attempts, fit_error(error), now_utc()))
                await cur.execute(delete, (job_id,))

    ###############
    ### Reports ###
    ###############

    async def pending(self, queue: str | None = None) -> int:
        """How many could be picked up right now: excludes delayed jobs that are not due and
        jobs another worker currently holds."""

        moment = now_utc()
        condition = sql.SQL("available_at <= %s AND (reserved_until IS NULL OR reserved_until <= %s)")
        params: tuple[Any, ...] = (moment, moment)

        if queue is not None:
            condition = sql.SQL("queue = %s AND ") + condition
            params = (queue, *params)

        statement = sql.SQL("SELECT count(*) FROM {rel} WHERE {cond}").format(
            rel=_qualified(self.table), cond=condition
        )

        async with self.conn.cursor() as cur:
            await cur.execute(statement, params)
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def stats(self) -> list[dict[str, Any]]:
        moment = now_utc()
        statement = sql.SQL(
            "SELECT queue,"
            "  count(*) FILTER (WHERE available_at <= %s"
            "    AND (reserved_until IS NULL OR reserved_until <= %s)) AS pending,"
            "  count(*) FILTER (WHERE available_at > %s) AS delayed,"
            "  count(*) FILTER (WHERE reserved_until > %s) AS reserved,"
            "  count(*) AS total "
            "FROM {rel} GROUP BY queue ORDER BY queue"
        ).format(rel=_qualified(self.table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (moment, moment, moment, moment))
            names = column_names(cur)
            return [dict(zip(names, row, strict=True)) for row in await cur.fetchall()]

    async def failed_count(self) -> int:
        statement = sql.SQL("SELECT count(*) FROM {rel}").format(rel=_qualified(self.failed_table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement)
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def failed_rows(self, limit: int) -> list[dict[str, Any]]:
        statement = sql.SQL(
            "SELECT id, failed_at, queue, name, attempts, error FROM {rel} ORDER BY failed_at DESC, id DESC LIMIT %s"
        ).format(rel=_qualified(self.failed_table))

        async with self.conn.cursor() as cur:
            await cur.execute(statement, (max(1, limit),))
            names = column_names(cur)
            return [dict(zip(names, row, strict=True)) for row in await cur.fetchall()]

    async def retry_failed(self, job_id: int | None, max_attempts: int) -> int:
        """Move failed jobs back onto the queue. Returns how many were requeued.

        One transaction per job rather than one for the batch: four hundred failed jobs
        should not be an all-or-nothing operation, and an interrupted run leaves the ones it
        already moved on the queue.
        """

        select = sql.SQL("SELECT id, queue, name, payload FROM {rel}").format(rel=_qualified(self.failed_table))
        params: tuple[Any, ...] = ()
        if job_id is not None:
            select = select + sql.SQL(" WHERE id = %s")
            params = (job_id,)

        async with self.conn.cursor() as cur:
            await cur.execute(select + sql.SQL(" ORDER BY id"), params)
            rows = await cur.fetchall()

        insert = sql.SQL(
            "INSERT INTO {rel} (queue, name, payload, attempts, max_attempts, priority, unique_key,"
            " available_at, last_error, created_at) "
            "VALUES (%s, %s, %s, 0, %s, 0, NULL, %s, '', %s)"
        ).format(rel=_qualified(self.table))
        delete = sql.SQL("DELETE FROM {rel} WHERE id = %s").format(rel=_qualified(self.failed_table))

        requeued = 0
        for failed_id, queue, name, payload in rows:
            moment = now_utc()
            async with self.conn.transaction():
                async with self.conn.cursor() as cur:
                    await cur.execute(insert, (queue, name, payload, max(1, max_attempts), moment, moment))
                    await cur.execute(delete, (failed_id,))
            requeued += 1

        return requeued

    async def forget_failed(self, job_id: int | None, before: str | None) -> int:
        statement = sql.SQL("DELETE FROM {rel}").format(rel=_qualified(self.failed_table))
        params: tuple[Any, ...] = ()

        if job_id is not None:
            statement = statement + sql.SQL(" WHERE id = %s")
            params = (job_id,)
        elif before is not None:
            statement = statement + sql.SQL(" WHERE failed_at < %s")
            params = (before,)

        # With neither argument this deletes everything, which is what --all means. The CLI
        # is what insists one of the three was given.
        async with self.conn.cursor() as cur:
            await cur.execute(statement, params)
            return cur.rowcount
