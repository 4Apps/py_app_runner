"""WebSocket user resolution from the per-message auth token."""

import logging
from typing import Any

import pytest

from py_app_runner.request_handler.auth_service import AuthService
from tests.api_keys_pg import UserRow, live_bridge, ws_call

pytestmark = pytest.mark.filterwarnings(
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning"
)


async def test_failed_user_lookup_is_retried_on_the_next_message(monkeypatch: pytest.MonkeyPatch):
    async with live_bridge("par_test_ws_auth") as bridge:
        user_id, public_id = await bridge.add_user()
        token = AuthService(logging.getLogger("test")).create_access_jwt(subject_public_id=public_id, token_type="user")

        lookup = UserRow.get_by_public_id
        failures = [ConnectionError("pool exhausted")]

        async def flaky_lookup(cls: type[UserRow], public_id: str, pg_cur: Any) -> Any:
            if failures:
                raise failures.pop()
            return await lookup(public_id, pg_cur)

        monkeypatch.setattr(UserRow, "get_by_public_id", classmethod(flaky_lookup))
        conn = await bridge.ws()

        assert "error" in await ws_call(conn, 1, "public", "anything", auth_token=token)
        assert (await ws_call(conn, 2, "public", "anything", auth_token=token))["user_id"] == user_id

        conn.close()
