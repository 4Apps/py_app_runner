"""User resolution from auth tokens: the per-message WebSocket token, and impersonation."""

import logging
import uuid
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


async def test_open_socket_drops_a_user_disabled_after_the_token_was_accepted():
    async with live_bridge("par_test_ws_reverify") as bridge:
        user_id, public_id = await bridge.add_user()
        token = AuthService(logging.getLogger("test")).create_access_jwt(subject_public_id=public_id, token_type="user")
        conn = await bridge.ws()

        assert (await ws_call(conn, 1, "public", "anything", auth_token=token))["user_id"] == user_id

        await bridge.conn.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (user_id,))

        # The token is sent once; the socket keeps it, and must keep checking it.
        assert (await ws_call(conn, 2, "public", "anything"))["user_id"] is None
        assert (await ws_call(conn, 3, "public", "anything", auth_token=token))["user_id"] is None
        assert str(user_id) not in bridge.connection_manager.user_connections.assigned.values()

        conn.close()


async def test_impersonation_token_needs_an_active_impersonator():
    async with live_bridge("par_test_impersonation") as bridge:
        target_id, target_public_id = await bridge.add_user()
        _, admin_public_id = await bridge.add_user()
        _, disabled_admin_public_id = await bridge.add_user(disabled=True)
        auth = AuthService(logging.getLogger("test"))

        cases = [
            (admin_public_id, target_id),
            (disabled_admin_public_id, None),
            (str(uuid.uuid4()), None),
        ]
        for impersonator, expected in cases:
            token = auth.create_impersonation_jwt(
                target_public_id=target_public_id, impersonator_public_id=impersonator
            )
            _, body = await bridge.call("public", "anything", headers={"Authorization": f"Bearer {token}"})

            assert body["data"]["user_id"] == expected, impersonator
