"""The Redis driver, on streams.

Streams rather than lists because a list hands a job out and forgets it, so the worker that
popped one and then died took it with it. A consumer group keeps every delivered entry until
somebody acknowledges it, and XAUTOCLAIM hands one back once it has gone idle longer than the
visibility timeout - which is the same "a claim is a deadline, not a flag" the database
driver gets from `reserved_until`.

What it cannot do is the thing the database driver exists for: a push here is a write to a
second system, so it cannot join the transaction that caused it, and no durability setting
on the redis side changes that. Reach for this when the volume genuinely warrants it, or
when losing a job would be survivable.

Key layout:

    {prefix}j:{id}            hash    the job; everything mutable lives here
    {prefix}q:{queue}:s:{n}   stream  ids that are ready, one stream per priority level
    {prefix}q:{queue}:levels  zset    which priorities that queue has streams for
    {prefix}q:{queue}:delayed zset    ids scored by the second they become due
    {prefix}u:{key}           string  the job id holding a unique key
    {prefix}failed            zset    ids scored by when they failed
    {prefix}queues            set     queue names, so status can enumerate them
    {prefix}seq               string  the id counter

A stream entry carries the job id and nothing else, because a stream entry cannot be edited.
The stream indexes what is ready; the hash is the job.

Needs Redis 6.2 or newer for XAUTOCLAIM. **Not cluster aware**: a job's keys span more than
one slot by design, and every script below addresses keys it builds itself rather than
declaring them, so a cluster would refuse them.
"""

import datetime
import json
from typing import Any

from py_app_runner.queue.driver_pg import encode_payload
from py_app_runner.queue.job import Job, QueueError

# One consumer name for the whole fleet. Naming consumers per host and pid would leak a dead
# consumer entry on every deploy, and idle time is tracked per entry rather than per
# consumer, so sharing the name costs nothing. Who actually holds a job is in `reserved_by`.
CONSUMER = "shared"

# How many due jobs a single reserve call promotes out of the delayed set. Bounded so that a
# backlog of a million scheduled jobs coming due at once does not turn one reserve into a
# long script that blocks the server.
PROMOTE_LIMIT = 50

MAX_ERROR_BYTES = 60000


def fit_error(error: str) -> str:
    raw = error.encode("utf-8")
    if len(raw) <= MAX_ERROR_BYTES:
        return error

    return raw[:MAX_ERROR_BYTES].decode("utf-8", errors="ignore") + "\n... truncated"


def stamp(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# ARGV: prefix, queue, name, payload, priority, max_attempts, unique, available_at,
#       created_at, now
_PUSH = """
local p, queue, uk = ARGV[1], ARGV[2], ARGV[7]

if uk ~= '' then
    local held = redis.call('GET', p .. 'u:' .. uk)
    if held then
        if redis.call('EXISTS', p .. 'j:' .. held) == 1 then
            return held
        end
        redis.call('DEL', p .. 'u:' .. uk)
    end
end

local id = redis.call('INCR', p .. 'seq')
local job = p .. 'j:' .. id
local level = ARGV[5]

redis.call('HSET', job,
    'queue', queue, 'name', ARGV[3], 'payload', ARGV[4],
    'attempts', '0', 'max_attempts', ARGV[6], 'priority', level,
    'unique_key', uk, 'available_at', ARGV[8], 'reserved_until', '0',
    'reserved_by', '', 'last_error', '', 'created_at', ARGV[9], 'entry', '')

if uk ~= '' then
    redis.call('SET', p .. 'u:' .. uk, id)
end

redis.call('SADD', p .. 'queues', queue)

if tonumber(ARGV[8]) > tonumber(ARGV[10]) then
    redis.call('ZADD', p .. 'q:' .. queue .. ':delayed', tonumber(ARGV[8]), id)
else
    local entry = redis.call('XADD', p .. 'q:' .. queue .. ':s:' .. level, '*', 'job', id)
    redis.call('HSET', job, 'entry', entry)
    redis.call('ZADD', p .. 'q:' .. queue .. ':levels', tonumber(level), level)
end

return tostring(id)
"""

# ARGV: prefix, queue, now, timeout, worker, group, consumer, promote_limit
#
# Promote what is due, then walk the priority levels highest first. XAUTOCLAIM runs before
# XREADGROUP on each level so a dead worker's job is retried before new work is started,
# rather than being left at the back of the queue behind everything pushed since.
_RESERVE = """
local p, queue = ARGV[1], ARGV[2]
local now, timeout = tonumber(ARGV[3]), tonumber(ARGV[4])
local worker, group, consumer = ARGV[5], ARGV[6], ARGV[7]

local levels = p .. 'q:' .. queue .. ':levels'
local delayed = p .. 'q:' .. queue .. ':delayed'

local due = redis.call('ZRANGEBYSCORE', delayed, '-inf', now, 'LIMIT', 0, tonumber(ARGV[8]))
for i = 1, #due do
    local id = due[i]
    -- Only the caller that actually removed it may add the stream entry, or two workers
    -- promoting at once would each add one for the same job.
    if redis.call('ZREM', delayed, id) == 1 then
        local job = p .. 'j:' .. id
        local level = redis.call('HGET', job, 'priority')
        if level then
            local entry = redis.call('XADD', p .. 'q:' .. queue .. ':s:' .. level, '*', 'job', id)
            redis.call('HSET', job, 'entry', entry)
            redis.call('ZADD', levels, tonumber(level), level)
        end
    end
end

local ranked = redis.call('ZREVRANGE', levels, 0, -1)
for i = 1, #ranked do
    local stream = p .. 'q:' .. queue .. ':s:' .. ranked[i]
    -- Created at 0 rather than $, because jobs are pushed long before any worker starts and
    -- $ would skip every one of them. pcall because it errors if the group already exists.
    redis.pcall('XGROUP', 'CREATE', stream, group, '0', 'MKSTREAM')

    local taken = nil
    local auto = redis.call('XAUTOCLAIM', stream, group, consumer, timeout * 1000, '0-0', 'COUNT', 1)
    if auto and auto[2] and auto[2][1] and auto[2][1][2] then
        taken = auto[2][1]
    end

    if not taken then
        local read = redis.call('XREADGROUP', 'GROUP', group, consumer,
            'COUNT', 1, 'STREAMS', stream, '>')
        if read and read[1] and read[1][2] and read[1][2][1] then
            taken = read[1][2][1]
        end
    end

    if taken then
        local entry, id = taken[1], taken[2][2]
        local job = p .. 'j:' .. id
        local until_ = tonumber(redis.call('HGET', job, 'reserved_until') or '0') or 0
        if redis.call('EXISTS', job) == 0 then
            -- The hash is gone: a tombstone entry for a job already completed. Drop it.
            redis.call('XACK', stream, group, entry)
            redis.call('XDEL', stream, entry)
        elseif until_ > now then
            -- Somebody still legitimately holds this one and XAUTOCLAIM handed it over
            -- early. Park it back in the delayed set until its claim runs out. Parking is
            -- not an attempt, so `attempts` is deliberately untouched here.
            redis.call('ZADD', p .. 'q:' .. queue .. ':delayed', until_, id)
            redis.call('HSET', job, 'entry', '')
            redis.call('XACK', stream, group, entry)
            redis.call('XDEL', stream, entry)
        else
            redis.call('HINCRBY', job, 'attempts', 1)
            redis.call('HSET', job, 'reserved_by', worker,
                'reserved_until', now + timeout, 'entry', entry)
            return {id, entry, redis.call('HGETALL', job)}
        end
    end
end

return nil
"""

# ARGV: prefix, queue, id, entry, group
_COMPLETE = """
local p, queue, id, entry, group = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local job = p .. 'j:' .. id
local level = redis.call('HGET', job, 'priority') or '0'
local stream = p .. 'q:' .. queue .. ':s:' .. level

redis.call('XACK', stream, group, entry)
redis.call('XDEL', stream, entry)

local uk = redis.call('HGET', job, 'unique_key')
if uk and uk ~= '' then
    redis.call('DEL', p .. 'u:' .. uk)
end

redis.call('DEL', job)
return 1
"""

# ARGV: prefix, queue, id, entry, group, available_at, error
_RELEASE = """
local p, queue, id, entry, group = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local job = p .. 'j:' .. id
local level = redis.call('HGET', job, 'priority') or '0'
local stream = p .. 'q:' .. queue .. ':s:' .. level

-- The delayed ZADD happens before the ACK and DEL on purpose: a crash between them leaves
-- the job in both places, which a later reserve resolves, rather than in neither.
redis.call('ZADD', p .. 'q:' .. queue .. ':delayed', tonumber(ARGV[6]), id)
redis.call('HSET', job, 'available_at', ARGV[6], 'reserved_until', '0',
    'reserved_by', '', 'last_error', ARGV[7], 'entry', '')

redis.call('XACK', stream, group, entry)
redis.call('XDEL', stream, entry)
return 1
"""

# ARGV: prefix, queue, id, entry, group, error, now, failed_at
_FAIL = """
local p, queue, id, entry, group = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local job = p .. 'j:' .. id
local level = redis.call('HGET', job, 'priority') or '0'
local stream = p .. 'q:' .. queue .. ':s:' .. level

redis.call('XACK', stream, group, entry)
redis.call('XDEL', stream, entry)

local uk = redis.call('HGET', job, 'unique_key')
if uk and uk ~= '' then
    redis.call('DEL', p .. 'u:' .. uk)
    redis.call('HSET', job, 'unique_key', '')
end

-- No copy to a second structure: the hash stays where it is and joins the failed set, so
-- `queue retry` puts back the job that failed rather than a reconstruction of it, and the
-- id never changes.
redis.call('HSET', job, 'error', ARGV[6], 'failed_at', ARGV[8],
    'reserved_until', '0', 'reserved_by', '', 'entry', '')
redis.call('ZADD', p .. 'failed', tonumber(ARGV[7]), id)
return 1
"""

# ARGV: prefix, id, max_attempts, now
_REQUEUE = """
local p, id = ARGV[1], ARGV[2]
local job = p .. 'j:' .. id

-- A job that was not in the failed set is not a failed job. Returning here rather than
-- carrying on is what stops `retry --id N` naming a live pending job from resetting it and
-- adding a second stream entry for it.
if redis.call('ZREM', p .. 'failed', id) == 0 then
    return 0
end

if redis.call('EXISTS', job) == 0 then
    return 0
end

local queue = redis.call('HGET', job, 'queue')
if not queue or queue == '' then queue = 'default' end

redis.call('HSET', job, 'attempts', '0', 'max_attempts', ARGV[3], 'priority', '0',
    'unique_key', '', 'available_at', ARGV[4], 'reserved_until', '0',
    'reserved_by', '', 'last_error', '', 'error', '', 'failed_at', '')

local entry = redis.call('XADD', p .. 'q:' .. queue .. ':s:0', '*', 'job', id)
redis.call('HSET', job, 'entry', entry)
redis.call('ZADD', p .. 'q:' .. queue .. ':levels', 0, '0')
redis.call('SADD', p .. 'queues', queue)
return 1
"""

# ARGV: prefix, id
_FORGET = """
local p, id = ARGV[1], ARGV[2]
if redis.call('ZREM', p .. 'failed', id) == 0 then
    return 0
end

redis.call('DEL', p .. 'j:' .. id)
return 1
"""


class RedisQueue:
    def __init__(self, redis_con: Any, prefix: str = "queue:", group: str = "workers") -> None:
        self.redis_con = redis_con
        self.prefix = prefix or "queue:"
        self.group = group or "workers"

        # register_script gives EVALSHA with an automatic fall back to EVAL when the server
        # has forgotten the script - after a restart, or a SCRIPT FLUSH - which is otherwise
        # a NOSCRIPT error the caller has to know to retry.
        self._push = redis_con.register_script(_PUSH)
        self._reserve = redis_con.register_script(_RESERVE)
        self._complete = redis_con.register_script(_COMPLETE)
        self._release = redis_con.register_script(_RELEASE)
        self._fail = redis_con.register_script(_FAIL)
        self._requeue = redis_con.register_script(_REQUEUE)
        self._forget = redis_con.register_script(_FORGET)

    ##############
    ### Naming ###
    ##############

    def _job_key(self, job_id: int | str) -> str:
        return f"{self.prefix}j:{job_id}"

    def _stream(self, queue: str, level: int | str) -> str:
        return f"{self.prefix}q:{queue}:s:{level}"

    def _levels(self, queue: str) -> str:
        return f"{self.prefix}q:{queue}:levels"

    def _delayed(self, queue: str) -> str:
        return f"{self.prefix}q:{queue}:delayed"

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
        moment = now_utc()
        now = int(moment.timestamp())

        result = await self._push(
            keys=[],
            args=[
                self.prefix,
                queue or "default",
                name,
                encode_payload(payload or {}),
                priority,
                max(1, max_attempts),
                unique or "",
                now + max(0, delay),
                stamp(moment),
                now,
            ],
        )

        return int(result)

    ###############
    ### Reserve ###
    ###############

    async def reserve(self, queues: list[str], timeout: int, worker: str) -> Job | None:
        for queue in queues:
            job = await self._reserve_from(queue, timeout, worker)
            if job is not None:
                return job

        return None

    async def _reserve_from(self, queue: str, timeout: int, worker: str) -> Job | None:
        now = int(now_utc().timestamp())

        result = await self._reserve(
            keys=[],
            args=[
                self.prefix,
                queue,
                now,
                max(1, timeout),
                worker[:64],
                self.group,
                CONSUMER,
                PROMOTE_LIMIT,
            ],
        )

        if not result:
            return None

        job_id, entry, flat = result
        fields = self._unflatten(flat)

        job = self._to_job(int(job_id), fields, str(entry))
        if job is None:
            # Undecodable payload. It will not decode on the next attempt either, so it goes
            # straight to failed rather than spending its whole budget rediscovering that.
            await self._fail_raw(
                job_id=int(job_id),
                queue=fields.get("queue") or queue,
                entry=str(entry),
                error="Payload is not valid JSON, so no handler could be given it.",
            )
            return None

        return job

    def _unflatten(self, flat: list[str]) -> dict[str, str]:
        """HGETALL comes back from Lua as a flat array, not a map."""

        return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}

    def _to_job(self, job_id: int, fields: dict[str, str], entry: str) -> Job | None:
        try:
            payload = json.loads(fields.get("payload") or "")
        except (TypeError, ValueError):
            return None

        if not isinstance(payload, dict):
            return None

        return Job(
            id=job_id,
            queue=fields.get("queue") or "default",
            name=fields.get("name") or "",
            payload=payload,
            payload_json=fields.get("payload") or "",
            # Floored at 1: a job being handed to a handler has been attempted at least once
            # by definition, and a hash edited by hand should not make that read as zero.
            attempts=max(1, int(fields.get("attempts") or 1)),
            max_attempts=max(1, int(fields.get("max_attempts") or 1)),
            handle=entry,
        )

    ##################
    ### Completion ###
    ##################

    async def delete(self, job: Job) -> None:
        await self._complete(keys=[], args=[self.prefix, job.queue, job.id, job.handle, self.group])

    async def release(self, job: Job, delay: int = 0, error: str = "") -> None:
        now = int(now_utc().timestamp())

        await self._release(
            keys=[],
            args=[
                self.prefix,
                job.queue,
                job.id,
                job.handle,
                self.group,
                now + max(0, delay),
                fit_error(error),
            ],
        )

    async def fail(self, job: Job, error: str) -> None:
        await self._fail_raw(job.id, job.queue, job.handle, error)

    async def _fail_raw(self, job_id: int, queue: str, entry: str, error: str) -> None:
        moment = now_utc()

        await self._fail(
            keys=[],
            args=[
                self.prefix,
                queue,
                job_id,
                entry,
                self.group,
                fit_error(error),
                int(moment.timestamp()),
                stamp(moment),
            ],
        )

    ###############
    ### Reports ###
    ###############

    async def _queue_names(self) -> list[str]:
        names = await self.redis_con.smembers(f"{self.prefix}queues")
        return sorted(str(name) for name in names)

    async def _counts(self, queue: str, now: int) -> dict[str, Any]:
        levels = await self.redis_con.zrevrange(self._levels(queue), 0, -1)

        ready = 0
        held = 0
        for level in levels:
            stream = self._stream(queue, level)
            entries = int(await self.redis_con.xlen(stream))
            ready += entries

            if entries == 0:
                # No entries means no pending list to read, and asking anyway would be a
                # round trip per empty priority level.
                continue

            # An absent group counts as zero rather than being created here: reporting on a
            # queue must not be what brings its consumer group into existence, which
            # XGROUP CREATE would.
            for info in await self.redis_con.xinfo_groups(stream):
                if info.get("name") == self.group:
                    held += int(info.get("pending") or 0)

        due = int(await self.redis_con.zcount(self._delayed(queue), "-inf", now))
        scheduled = int(await self.redis_con.zcard(self._delayed(queue)))

        pending = max(0, ready - held) + due
        delayed = max(0, scheduled - due)

        return {
            "queue": queue,
            "pending": pending,
            "delayed": delayed,
            "reserved": held,
            "total": pending + delayed + held,
        }

    async def pending(self, queue: str | None = None) -> int:
        now = int(now_utc().timestamp())
        queues = [queue] if queue is not None else await self._queue_names()

        total = 0
        for name in queues:
            total += (await self._counts(name, now))["pending"]

        return total

    async def stats(self) -> list[dict[str, Any]]:
        now = int(now_utc().timestamp())

        rows = []
        for name in await self._queue_names():
            counts = await self._counts(name, now)
            if counts["total"] > 0:
                rows.append(counts)

        return rows

    async def failed_count(self) -> int:
        return int(await self.redis_con.zcard(f"{self.prefix}failed"))

    async def failed_rows(self, limit: int) -> list[dict[str, Any]]:
        ids = await self.redis_con.zrevrange(f"{self.prefix}failed", 0, max(1, limit) - 1)

        rows = []
        for job_id in ids:
            fields = await self.redis_con.hgetall(self._job_key(job_id))
            if not fields:
                # The hash went while we were reading. Skipped rather than reported as a
                # row of blanks.
                continue

            rows.append(
                {
                    "id": int(job_id),
                    "failed_at": fields.get("failed_at") or "",
                    "queue": fields.get("queue") or "",
                    "name": fields.get("name") or "",
                    "attempts": int(fields.get("attempts") or 0),
                    "error": fields.get("error") or "",
                }
            )

        return rows

    async def retry_failed(self, job_id: int | None, max_attempts: int) -> int:
        now = int(now_utc().timestamp())
        ids = [job_id] if job_id is not None else await self.redis_con.zrange(f"{self.prefix}failed", 0, -1)

        requeued = 0
        for one in ids:
            moved = await self._requeue(keys=[], args=[self.prefix, one, max(1, max_attempts), now])
            requeued += int(moved)

        return requeued

    async def forget_failed(self, job_id: int | None, before: str | None) -> int:
        if job_id is not None:
            ids: list[Any] = [job_id]
        elif before is not None:
            ids = await self.redis_con.zrangebyscore(f"{self.prefix}failed", "-inf", f"({self._epoch(before)}")
        else:
            ids = await self.redis_con.zrange(f"{self.prefix}failed", 0, -1)

        removed = 0
        for one in ids:
            removed += int(await self._forget(keys=[], args=[self.prefix, one]))

        return removed

    def _epoch(self, before: str) -> int:
        """Read a --before date as UTC.

        Reading it as local time would silently move the cut-off by the offset, and the only
        symptom is rows that should have gone still being there - or worse, rows that should
        have stayed being gone.
        """

        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.datetime.strptime(before, pattern)
            except ValueError:
                continue

            return int(parsed.replace(tzinfo=datetime.UTC).timestamp())

        raise QueueError(f"{before!r} is not a date this queue can read.")
