"""Scoped API keys at the bridge boundary: real handlers, real socket, real Postgres."""

import datetime as dt
import ipaddress
import logging

import pytest

from py_app_runner.request_handler.auth_service import AuthService
from tests.api_keys_pg import live_bridge, ws_call

# database_wrapper's pool checkout wraps the connection fd in socket.fromfd() and never
# closes the duplicate; under -W error that ResourceWarning fails the test.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning"
)

NET_10 = [ipaddress.ip_network("10.0.0.0/8")]


async def test_abilities_decide_which_calls_a_key_may_make():
    async with live_bridge("par_test_key_abilities") as bridge:
        cases = [
            (["reports:run"], "reports", "run", 200),
            (["reports:run"], "reports", "delete", 403),
            (["reports:*"], "reports", "delete", 200),
            (["reports:*"], "db", "settings", 403),
            (["db:settings"], "db", "settings", 200),
            (["*"], "db", "settings", 200),
            ([], "reports", "run", 403),
        ]
        for abilities, service, action, expected in cases:
            key = await bridge.add_key(abilities)
            status, body = await bridge.call(service, action, key)

            assert status == expected, (abilities, service, action, body)
            if expected == 403:
                assert body["data"]["error"]["code"] == 1403

        status, body = await bridge.call("reports", "run", await bridge.add_key(["reports:run"], name="narrow"))
        assert body["data"]["key"] == "narrow"
        assert body["data"]["can_export"] is False

        status, body = await bridge.call("reports", "run", await bridge.add_key(["reports:*"]))
        assert body["data"]["can_export"] is True


async def test_disabled_expired_and_unknown_keys_are_refused():
    async with live_bridge("par_test_key_states") as bridge:
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
        valid = await bridge.add_key(["*"], expires_at=future)
        prefix = valid.split(".")[0]

        cases = [
            (valid, 200),
            (await bridge.add_key(["*"], disabled_at=past), 401),
            (await bridge.add_key(["*"], expires_at=past), 401),
            (f"{prefix}.wrong-secret", 401),
            ("akunknown.secret", 401),
            ("no-dot-at-all", 401),
        ]
        for key, expected in cases:
            status, body = await bridge.call("reports", "run", key)

            assert status == expected, (key, body)
            if expected == 401:
                assert body["data"]["error"]["code"] == 1401

        async with bridge.conn.cursor() as cur:
            await cur.execute(
                "SELECT total_uses, last_used_at IS NOT NULL FROM api_keys WHERE key_prefix = %s", (prefix,)
            )
            assert await cur.fetchone() == (1, True)


async def test_allowed_ips_trust_forwarding_headers_only_from_configured_proxies():
    async with live_bridge("par_test_key_ips") as bridge:
        key = await bridge.add_key(["*"], allowed_ips=NET_10)
        local_key = await bridge.add_key(["*"], allowed_ips=[ipaddress.ip_network("127.0.0.1/32")])

        cases = [
            # (trusted_proxies, X-Forwarded-For, key, expected)
            ([], None, key, 401),
            # xheaders=True already rewrote remote_ip from this header; it must not count.
            ([], "10.1.2.3", key, 401),
            (["127.0.0.1"], "10.1.2.3", key, 200),
            (["127.0.0.1"], "10.1.2.3, 11.0.0.1", key, 401),
            ([], None, local_key, 200),
        ]
        for trusted, forwarded_for, api_key, expected in cases:
            bridge.config["trusted_proxies"] = trusted
            headers = {"X-Forwarded-For": forwarded_for, "X-Real-IP": forwarded_for} if forwarded_for else {}
            status, body = await bridge.call("reports", "run", api_key, headers=headers)

            assert status == expected, (trusted, forwarded_for, body)


async def test_key_acts_as_its_user_unless_a_token_is_presented():
    async with live_bridge("par_test_key_user") as bridge:
        key_user, _ = await bridge.add_user()
        jwt_user, jwt_public_id = await bridge.add_user()
        disabled_user, _ = await bridge.add_user(disabled=True)
        key = await bridge.add_key(["reports:*"], user_id=key_user)

        _, body = await bridge.call("reports", "run", key)
        assert body["data"]["user_id"] == key_user

        token = AuthService(logging.getLogger("test")).create_access_jwt(
            subject_public_id=jwt_public_id, token_type="user"
        )
        _, body = await bridge.call("reports", "run", key, headers={"Authorization": f"Bearer {token}"})
        assert body["data"]["user_id"] == jwt_user

        # A token that does not verify is still a token: no fallback to the key's user.
        _, body = await bridge.call("reports", "run", key, headers={"Authorization": "Bearer expired.or.forged"})
        assert body["data"]["user_id"] is None

        status, body = await bridge.call("reports", "run", await bridge.add_key(["*"], user_id=disabled_user))
        assert status == 401
        assert body["data"]["error"]["code"] == 1401


async def test_services_not_requiring_a_key_ignore_it():
    async with live_bridge("par_test_key_open") as bridge:
        user_id, _ = await bridge.add_user()
        key = await bridge.add_key([], user_id=user_id)

        for api_key in (None, key, "garbage"):
            status, body = await bridge.call("public", "anything", api_key)

            assert status == 200
            assert body["data"]["user_id"] is None
            assert body["data"]["key"] is None


async def test_websocket_scopes_every_message_and_notices_revocation():
    async with live_bridge("par_test_key_ws") as bridge:
        user_id, _ = await bridge.add_user()
        key = await bridge.add_key(["reports:run"], user_id=user_id)
        conn = await bridge.ws(key)

        reply = await ws_call(conn, 1, "reports", "run")
        assert reply["user_id"] == user_id
        assert str(user_id) in bridge.connection_manager.user_connections.assigned.values()

        assert (await ws_call(conn, 2, "reports", "delete"))["error"]["code"] == 1403

        # The key's user belongs to calls the key authorises, not to open services.
        assert (await ws_call(conn, 3, "public", "anything"))["user_id"] is None

        await bridge.conn.execute("UPDATE api_keys SET abilities = '{reports:*}'")
        assert (await ws_call(conn, 4, "reports", "delete"))["action"] == "delete"

        await bridge.conn.execute("UPDATE api_keys SET disabled_at = now()")
        assert (await ws_call(conn, 5, "reports", "run"))["error"]["code"] == 1401
        assert (await ws_call(conn, 6, "reports", "run"))["error"]["code"] == 1401

        conn.close()
