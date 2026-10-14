from typing import Any, cast

from database_wrapper_pgsql import DBWrapperPgsqlAsync, PgsqlWithPoolingAsync
from database_wrapper_redis import RedisDbWithPoolAsync
from database_wrapper_sqlite import DBWrapperSqliteAsync, SqliteConnectionAsync, SqliteWithPoolingAsync

PGSQL = "pgsql"
SQLITE = "sqlite"


def db_driver(db_config: dict[str, Any] | None) -> str:
    """`config["db"][name]["driver"]`, Postgres when absent."""

    driver = (db_config or {}).get("driver") or PGSQL
    if driver not in (PGSQL, SQLITE):
        raise ValueError(f"Unknown database driver {driver!r}; expected {PGSQL!r} or {SQLITE!r}")

    return driver


def redis_configured(config: dict[str, Any]) -> bool:
    return bool(((config.get("db") or {}).get("redis") or {}).get("hostname"))


def make_main_db_pool(db_config: dict[str, Any], instance_name: str) -> PgsqlWithPoolingAsync | SqliteWithPoolingAsync:
    if db_driver(db_config) == SQLITE:
        return SqliteWithPoolingAsync(cast(Any, db_config), instance_name=instance_name)

    return PgsqlWithPoolingAsync(db_config=cast(Any, db_config), instance_name=instance_name)


def make_db_wrapper(conn: Any, cur: Any) -> DBWrapperPgsqlAsync | DBWrapperSqliteAsync:
    if isinstance(conn, SqliteConnectionAsync):
        return DBWrapperSqliteAsync(cur)

    return DBWrapperPgsqlAsync(cur)


class DbPools:
    """
    Concrete class to hold database connection pools and wrappers for use
    throughout the application
    """

    cache_db_pool: RedisDbWithPoolAsync | None
    # Annotated as the Postgres pool so existing consumers keep their types. On SQLite it
    # holds a SqliteWithPoolingAsync, which yields the same (conn, cur) pair.
    main_db_pool: PgsqlWithPoolingAsync

    def __init__(
        self,
        cache_db_pool: RedisDbWithPoolAsync | None,
        main_db_pool: PgsqlWithPoolingAsync | SqliteWithPoolingAsync,
    ):
        self.cache_db_pool = cache_db_pool
        self.main_db_pool = cast(PgsqlWithPoolingAsync, main_db_pool)
