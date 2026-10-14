"""Which backends the bridge needs, decided from config and routes before any worker forks."""

from typing import Any

import pytest
from tornado.web import RequestHandler

from py_app_runner.bridge._service import backend_config_error, uses_websockets
from py_app_runner.bridge.websocket import BaseWebSocketHandler
from py_app_runner.registry import AppRegistry

PG = {"hostname": "db", "username": "u", "password": "p", "database": "d"}
SQLITE = {"driver": "sqlite", "database": "/tmp/app.sqlite3"}
REDIS = {"hostname": "cache"}


@pytest.fixture
def registry():
    original = AppRegistry.config()
    yield
    AppRegistry.configure(config=original, users_model=object, api_keys_model=object)


def test_websocket_routes_are_detected():
    assert uses_websockets([("/ws", BaseWebSocketHandler, "ws"), ("/api", RequestHandler, "api")])
    assert not uses_websockets([("/api", RequestHandler, "api")])
    assert not uses_websockets(None)


@pytest.mark.parametrize(
    ("main", "redis", "websockets", "api_key_use_db", "error"),
    [
        (PG, REDIS, True, True, None),
        (PG, {}, False, False, None),
        (SQLITE, {}, False, False, None),
        (PG, {}, True, False, "WebSocket routes"),
        ({**PG, "driver": "mysql"}, REDIS, False, False, "Unknown database driver"),
        (SQLITE, REDIS, False, True, "api_key_use_db needs Postgres"),
    ],
)
def test_backend_config_error(registry, main: dict[str, Any], redis, websockets, api_key_use_db, error):
    config = {"db": {"main": main, "redis": redis}}
    AppRegistry.configure(config=config, users_model=object, api_key_use_db=api_key_use_db)

    result = backend_config_error(config, websockets)

    if error is None:
        assert result is None
    else:
        assert result is not None and error in result
