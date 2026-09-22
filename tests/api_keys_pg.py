"""A live bridge (HTTP + WebSocket) over a throwaway database, for the API key tests.

Runs the real ApiHandler and WebSocketHandler on a real socket with `xheaders=True`, as
production does, so the client-IP tests see what a deployed bridge would.
"""

import contextlib
import pathlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
from database_wrapper import DBDefaultsDataModel, MetadataDict
from database_wrapper_pgsql import DBWrapperPgsqlAsync, PgsqlWithPoolingAsync
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.websocket import WebSocketClientConnection, websocket_connect

import py_app_runner.api_keys as api_keys_package
from py_app_runner.api_keys.keys import generate_key, hash_secret
from py_app_runner.bridge.api import ApiHandler
from py_app_runner.bridge.websocket import WebSocketHandler
from py_app_runner.db_pools import DbPools
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import WebApplicationBase
from py_app_runner.utils import json_decode, json_encode
from tests.migrations_pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, pg_dsn_for

PEPPER = "test-pepper"
JWT_SECRET = "test-jwt-secret-long-enough-for-hs256"

SCHEMA = (pathlib.Path(api_keys_package.__file__).parent / "files" / "install.pgsql.sql").read_text()
USERS_SCHEMA = """
CREATE TABLE users (
    id          bigserial PRIMARY KEY,
    public_id   uuid NOT NULL DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz,
    deleted_at  timestamptz
);
"""


@dataclass
class UserRow(DBDefaultsDataModel):
    @property
    def table_name(self) -> str:
        return "users"

    public_id: Any = field(
        default=None,
        metadata=MetadataDict(db_field=("public_id", "uuid"), store=True, update=False),
    )

    @classmethod
    async def get_by_public_id(cls, public_id: str, pg_cur: Any) -> Any:
        return await DBWrapperPgsqlAsync(pg_cur).get_by_key(cls(), id_key="public_id", id_value=public_id)


class WhoAmI:
    """A service that reports what the bridge handed it."""

    def __init__(self, requires_api_key: bool | None = None):
        self.requires_api_key = requires_api_key

    async def bridge_request(self, action: str, request_data: dict[str, Any], bridge_handler: Any) -> Any:
        user = bridge_handler.current_user
        key = bridge_handler.current_api_key
        return {
            "action": action,
            "user_id": user.id if user else None,
            "key": key.name if key else None,
            "can_export": bridge_handler.key_can("reports:export"),
        }


class FakeUserConnections:
    def __init__(self) -> None:
        self.assigned: dict[str, str] = {}

    def add_connection(self, conn: Any) -> None:
        pass

    def remove_connection(self, conn: Any) -> None:
        pass

    def assign_user_id(self, conn_id: str, user_id: str) -> None:
        self.assigned[conn_id] = user_id

    def unassign_connection(self, conn_id: str, keep_user_id: str | None = None) -> None:
        self.assigned.pop(conn_id, None)


class FakeConnectionManager:
    def __init__(self) -> None:
        self.user_connections = FakeUserConnections()


class EagerRecheckWebSocketHandler(WebSocketHandler):
    api_key_recheck_seconds = 0


@dataclass
class Bridge:
    port: int
    conn: psycopg.AsyncConnection
    connection_manager: FakeConnectionManager

    @property
    def config(self) -> dict[str, Any]:
        return AppRegistry.config()

    async def add_user(self, disabled: bool = False) -> tuple[int, str]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO users (disabled_at) VALUES (CASE WHEN %s THEN now() END) RETURNING id, public_id",
                (disabled,),
            )
            row = await cur.fetchone()
        assert row
        return row[0], str(row[1])

    async def add_key(self, abilities: list[str], name: str = "test", **columns: Any) -> str:
        prefix, secret = generate_key()
        values = {
            "name": name,
            "key_prefix": prefix,
            "secret_hash": hash_secret(secret, PEPPER),
            "abilities": abilities,
            **columns,
        }
        names = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        await self.conn.execute(f"INSERT INTO api_keys ({names}) VALUES ({placeholders})", list(values.values()))
        return f"{prefix}.{secret}"

    async def call(
        self,
        service: str,
        action: str,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if key:
            request_headers["X-API-Key"] = key

        response = await AsyncHTTPClient().fetch(
            f"http://127.0.0.1:{self.port}/api/{service}",
            method="POST",
            body=json_encode({"action": action, "data": {}}),
            headers=request_headers,
            raise_error=False,
        )
        body = json_decode(response.body) if response.body else None
        return response.code, body

    async def ws(self, key: str | None = None) -> WebSocketClientConnection:
        headers = {"X-API-Key": key} if key else {}
        return await websocket_connect(HTTPRequest(f"ws://127.0.0.1:{self.port}/ws", headers=headers))


async def ws_call(conn: WebSocketClientConnection, msg_id: int, service: str, action: str, **extra: Any) -> Any:
    await conn.write_message(json_encode({"msg_id": msg_id, "service": service, "data": {"action": action}, **extra}))
    reply = await conn.read_message()
    assert isinstance(reply, str)
    return json_decode(reply)["data"]


@contextlib.asynccontextmanager
async def live_bridge(db_name: str, services: dict[str, Any] | None = None) -> AsyncIterator[Bridge]:
    saved = (AppRegistry.config(), AppRegistry._users_model_cls, AppRegistry.api_key_use_db())

    async with pg_dsn_for(db_name) as dsn:
        db_config = {
            "hostname": PG_HOST,
            "port": PG_PORT,
            "username": PG_USER,
            "password": PG_PASSWORD,
            "database": db_name,
        }
        AppRegistry.configure(
            config={
                "environment": "test",
                "api_key_pepper": PEPPER,
                "jwt": {"secret": JWT_SECRET},
                "db": {"main": db_config},
                "trusted_proxies": [],
            },
            users_model=UserRow,
            api_key_use_db=True,
        )

        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await conn.execute(SCHEMA)
        await conn.execute(USERS_SCHEMA)

        pool = PgsqlWithPoolingAsync(db_config=dict(db_config), instance_name=f"test_{uuid.uuid4().hex[:8]}")  # type: ignore[arg-type]
        await pool.open_pool()

        app = WebApplicationBase([(r"/api/([^/]+)", ApiHandler), (r"/ws", EagerRecheckWebSocketHandler)])
        pybridge = PyBridge()
        for name, service in (services or default_services()).items():
            pybridge.cache_service(name, service)
        app.set_pybridge(pybridge)
        app.set_db_pools(DbPools(cache_db_pool=None, main_db_pool=pool))  # type: ignore[arg-type]
        connection_manager = FakeConnectionManager()
        app.set_wb_connection_manager(connection_manager)  # type: ignore[arg-type]

        sock, port = bind_unused_port()
        server = HTTPServer(app, xheaders=True)
        server.add_sockets([sock])

        try:
            yield Bridge(port=port, conn=conn, connection_manager=connection_manager)
        finally:
            server.stop()
            await server.close_all_connections()
            await pool.close_pool()
            await conn.close()
            AppRegistry.configure(config=saved[0], users_model=saved[1] or object, api_key_use_db=saved[2])


def default_services() -> dict[str, Any]:
    return {"reports": WhoAmI(), "db": WhoAmI(), "public": WhoAmI(requires_api_key=False)}
