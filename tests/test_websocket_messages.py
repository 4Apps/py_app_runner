"""What the WebSocket bridge does with a frame before any service sees it."""

import logging
import random
import string

import pytest
from tornado.websocket import WebSocketClientConnection

from py_app_runner.utils import json_decode, json_encode
from tests.api_keys_pg import live_bridge

pytestmark = pytest.mark.filterwarnings(
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning"
)


async def reply(conn: WebSocketClientConnection, frame: str) -> dict:
    await conn.write_message(frame)
    raw = await conn.read_message()
    assert isinstance(raw, str)
    return json_decode(raw)


async def test_unparseable_frame_is_logged_as_a_bounded_prefix(caplog: pytest.LogCaptureFixture):
    garbage = "{" + "".join(random.Random(0).choices(string.printable, k=200_000))

    async with live_bridge("par_test_ws_garbage") as bridge:
        conn = await bridge.ws()
        with caplog.at_level(logging.DEBUG, logger="py_app_runner"):
            response = await reply(conn, garbage)
        conn.close()

    assert response["data"]["error"]["code"] == 1000
    assert caplog.records
    assert max(len(record.getMessage()) for record in caplog.records) < 1000
    assert all(record.levelno < logging.ERROR for record in caplog.records)


async def test_incomplete_message_names_what_is_missing_without_echoing_it(caplog: pytest.LogCaptureFixture):
    message = {
        "service": "public",
        "auth_token": "token-in-message",
        "data": {"action": "login", "data": {"password": "hunter2"}},
    }

    async with live_bridge("par_test_ws_incomplete") as bridge:
        conn = await bridge.ws()
        with caplog.at_level(logging.DEBUG, logger="py_app_runner"):
            response = await reply(conn, json_encode(message))
        conn.close()

    assert response["data"]["error"] == {"code": 1001, "msg": "Missing input data: msg_id"}
    for secret in ("token-in-message", "hunter2"):
        assert secret not in caplog.text, secret


async def test_msg_id_zero_is_a_valid_id():
    async with live_bridge("par_test_ws_msg_id_zero") as bridge:
        conn = await bridge.ws()
        response = await reply(conn, json_encode({"msg_id": 0, "service": "public", "data": {"action": "anything"}}))
        conn.close()

    assert response["msg_id"] == 0
    assert response["data"]["action"] == "anything"
