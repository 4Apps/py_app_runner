"""The decorators on a SQLite main database. The connection, pool and wrapper themselves
are database_wrapper_sqlite's and tested there."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from database_wrapper import DBDataModel, MetadataDict
from database_wrapper_sqlite import SqliteWithPoolingAsync

from py_app_runner.db_pools import DbPools
from py_app_runner.http_exception import HTTPException
from py_app_runner.request_handler.decorators import with_cache, with_db, with_tx
from py_app_runner.request_handler.handlers import RequestHandlerHelper


@pytest.fixture
def db_config(tmp_path) -> dict[str, Any]:
    return {"driver": "sqlite", "database": str(tmp_path / "app.sqlite3")}


async def scalar(conn, statement: str) -> Any:
    cur = await conn.execute(statement)
    row = await cur.fetchone()
    return next(iter(row.values())) if row else None


@dataclass
class NoteModel(DBDataModel):
    @property
    def table_name(self) -> str:
        return "notes"

    body: str = field(default="", metadata=MetadataDict(db_field=("body", "text"), store=True, update=True))


class Notes(RequestHandlerHelper):
    @with_db
    @with_tx
    async def add(self, input_data: dict[str, Any]) -> dict[str, Any]:
        handles = {name: hasattr(self, name) for name in ("db_conn", "db_cur", "pg_conn", "pg_cur")}
        await self.db_wrapper.insert(NoteModel(body=input_data["body"]))
        if input_data.get("fail"):
            raise HTTPException("rejected", 4000)
        return handles

    @with_cache
    async def cached(self, input_data: dict[str, Any]) -> None:
        return None


def notes_handler(pool: SqliteWithPoolingAsync) -> Notes:
    handler = Notes()
    handler.bridge_handler = SimpleNamespace(db_pools=DbPools(cache_db_pool=None, main_db_pool=pool))  # type: ignore[assignment]
    return handler


class TestDecorators:
    async def test_with_db_and_with_tx_run_on_sqlite(self, db_config):
        pool = SqliteWithPoolingAsync(db_config)
        await pool.open_pool()
        async with pool as (conn, _cur):
            await conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")

        handler = notes_handler(pool)
        handles = await handler.add({"body": "kept"})
        with pytest.raises(HTTPException):
            await handler.add({"body": "rolled back", "fail": True})

        assert handles == {"db_conn": True, "db_cur": True, "pg_conn": False, "pg_cur": False}
        assert not hasattr(handler, "db_conn")
        async with pool as (conn, _cur):
            assert await scalar(conn, "SELECT group_concat(body) FROM notes") == "kept"

    async def test_with_cache_without_redis_is_a_clear_error(self, db_config):
        with pytest.raises(HTTPException, match="Cache is not configured"):
            await notes_handler(SqliteWithPoolingAsync(db_config)).cached({})
