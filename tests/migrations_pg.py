"""Throwaway-database helper for the migration integration tests.

Uses the project's own Postgres over the compose network rather than testcontainers, which
needs a Docker socket the develop container does not expose.

An unreachable Postgres is a **failure**, not a skip: skipping by default meant a CI run that
started before Postgres accepted connections quietly tested nothing and reported green. Set
`ALLOW_PG_SKIP` in the environment to opt into the old behaviour on a bare checkout with no
database at all. psycopg is a hard dependency of this package, so it is imported outright
rather than through `importorskip`, which would hide a broken install the same way.
"""

import contextlib
import os
from collections.abc import AsyncIterator

import psycopg
import pytest

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
        if not os.environ.get("ALLOW_PG_SKIP"):
            raise

        pytest.skip(f"Postgres unreachable at {PG_HOST}:{PG_PORT} (ALLOW_PG_SKIP is set): {e}")
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
