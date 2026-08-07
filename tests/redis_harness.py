"""Throwaway-keyspace helper for the tests that need a real Redis.

Mirrors migrations_pg.py, including its policy: an unreachable Redis is a **failure**, not
a skip, because skipping by default means a CI run that started before the server accepted
connections quietly tests nothing and reports green. Set `ALLOW_REDIS_SKIP` to opt out on a
bare checkout with no server at all.

Each test gets its own numbered database and flushes it on the way in and out, rather than
a key prefix. Throttle hashes its keys and the queue driver spreads a job across a hash, a
stream and two sorted sets, so "delete everything this test made" is not something a prefix
sweep can do reliably.
"""

import contextlib
import os
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as redis

REDIS_HOST = os.environ.get("TEST_REDIS_HOST", "py_app_runner_cache")
REDIS_PORT = int(os.environ.get("TEST_REDIS_PORT", "6379"))


@contextlib.asynccontextmanager
async def redis_for(database: int = 1) -> AsyncIterator[redis.Redis]:
    con = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=database,
        decode_responses=True,
        socket_connect_timeout=3,
    )

    try:
        await con.ping()
    except Exception as e:
        await con.aclose()
        if not os.environ.get("ALLOW_REDIS_SKIP"):
            raise

        pytest.skip(f"Redis unreachable at {REDIS_HOST}:{REDIS_PORT} (ALLOW_REDIS_SKIP is set): {e}")
        return

    try:
        await con.flushdb()
        yield con
    finally:
        await con.flushdb()
        await con.aclose()
