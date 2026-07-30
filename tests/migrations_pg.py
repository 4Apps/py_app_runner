"""Throwaway-database helper for the migration integration tests.

Uses the project's own Postgres over the compose network rather than testcontainers, which
needs a Docker socket the develop container does not expose. Skips cleanly when no Postgres
is reachable, so `pytest` still passes on a bare checkout.
"""

import contextlib
import os
from collections.abc import AsyncIterator

import pytest

psycopg = pytest.importorskip("psycopg")

PG_HOST = os.environ.get("TEST_PG_HOST", "py_app_runner_database")
PG_PORT = int(os.environ.get("TEST_PG_PORT", "5432"))
PG_USER = os.environ.get("TEST_PG_USER", "postgres")
PG_PASSWORD = os.environ.get("TEST_PG_PASSWORD", "postgres")


def dsn(database: str) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} "
        f"dbname={database} connect_timeout=3"
    )


@contextlib.asynccontextmanager
async def pg_dsn_for(db_name: str) -> AsyncIterator[str]:
    try:
        admin = await psycopg.AsyncConnection.connect(dsn("postgres"), autocommit=True)
    except Exception as e:
        pytest.skip(f"Postgres unreachable at {PG_HOST}:{PG_PORT}: {e}")
        return

    async with admin:
        async with admin.cursor() as cur:
            await cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            await cur.execute(f'CREATE DATABASE "{db_name}"')
        try:
            yield dsn(db_name)
        finally:
            async with admin.cursor() as cur:
                await cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
