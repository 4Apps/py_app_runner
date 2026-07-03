import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from py_app_runner.registry import AppRegistry
from py_app_runner.wbcm.device_connections import DeviceConnections
from py_app_runner.wbcm.factory import UserConnections
from py_app_runner.wbcm.wb_connection_manager import WbConnectionManager

TEST_STREAM = "test:socket_messages"


class FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class FakeConn:
    def __init__(self, uid: str, sid: str | None = None):
        self.uid = uid
        self.current_session_sid = sid
        self.loop = FakeLoop()
        self.closed = False
        self.messages: list[Any] = []

    def close(self, code=None, reason=None):
        self.closed = True

    def write_custom_message(self, message, wrap_in_data=True, **kwargs):
        self.messages.append(message)


def configure_registry() -> None:
    AppRegistry.configure(
        config={"environment": "test", "debug": False},
        users_model=object,
        api_keys_model=object,
        redis_channel=TEST_STREAM,
    )


def make_manager() -> WbConnectionManager:
    configure_registry()
    return WbConnectionManager(cache_db=MagicMock())


def revoke_message(user_id: str = "1", source_uid: str = "src", target_sid: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "service": "account",
        "type": "session_revoked",
        "data": "{}",
        "user_id": user_id,
        "source_uid": source_uid,
    }
    if target_sid is not None:
        message["target_sid"] = target_sid
    return message


class TestSessionRevoked:
    def test_revoke_closes_all_user_sockets_except_source(self):
        cm = make_manager()
        source = FakeConn("src", sid="sid1")
        other_a = FakeConn("a", sid="sid2")
        other_b = FakeConn("b", sid="sid3")
        for conn in (source, other_a, other_b):
            cm.user_connections.add_connection(conn)
            cm.user_connections.assign_user_id(conn.uid, "1")

        assert cm.work(revoke_message()) is True
        assert not source.closed
        assert other_a.closed
        assert other_b.closed

    def test_revoke_with_target_sid_closes_only_that_session(self):
        cm = make_manager()
        source = FakeConn("src", sid="sid1")
        target = FakeConn("a", sid="sid2")
        bystander = FakeConn("b", sid="sid3")
        for conn in (source, target, bystander):
            cm.user_connections.add_connection(conn)
            cm.user_connections.assign_user_id(conn.uid, "1")

        assert cm.work(revoke_message(target_sid="sid2")) is True
        assert not source.closed
        assert target.closed
        assert not bystander.closed

    def test_revoke_without_user_id_is_rejected(self):
        cm = make_manager()
        message = revoke_message()
        del message["user_id"]
        assert cm.work(message) is False


class TestConnectionMaps:
    def test_assign_user_id_deduplicates(self):
        uc = UserConnections()
        conn = FakeConn("a")
        uc.add_connection(conn)
        uc.assign_user_id("a", "1")
        uc.assign_user_id("a", "1")
        assert uc.user_id_map["1"] == ["a"]

    def test_remove_connection_cleans_empty_user_entry(self):
        uc = UserConnections()
        conn = FakeConn("a")
        uc.add_connection(conn)
        uc.assign_user_id("a", "1")
        uc.remove_connection(conn)
        assert "1" not in uc.user_id_map
        assert "a" not in uc.user_connections

    def test_assign_device_id_deduplicates_and_cleans_up(self):
        dc = DeviceConnections()
        conn = FakeConn("a")
        dc.add_connection(conn)
        dc.assign_device_id("a", "dev1")
        dc.assign_device_id("a", "dev1")
        assert dc.device_id_map["dev1"] == ["a"]
        dc.remove_connection(conn)
        assert "dev1" not in dc.device_id_map


class TestStreamFanOut:
    def _fake_cache_db(self, entries: list[tuple[str, dict[str, Any]]]) -> MagicMock:
        cache_db = MagicMock()
        cache_db.connection.xread = AsyncMock(return_value={TEST_STREAM: [entries]})
        return cache_db

    def test_all_managers_receive_every_message(self):
        # Plain XREAD (no consumer group): every forked worker's manager reads the
        # full stream, so a message reaches users on every worker.
        configure_registry()
        entries = [("1-1", {"service": "s", "type": "t", "data": "{}", "user_id": "1", "source_uid": "src"})]

        delivered: list[str] = []
        managers: list[WbConnectionManager] = []
        for name in ("worker_a", "worker_b"):
            cm = WbConnectionManager(cache_db=self._fake_cache_db(entries))
            conn = FakeConn(f"conn_{name}")
            conn.messages = delivered  # type: ignore[assignment]
            cm.user_connections.add_connection(conn)
            cm.user_connections.assign_user_id(conn.uid, "1")
            managers.append(cm)

        for cm in managers:
            asyncio.run(cm.process_messages())

        assert len(delivered) == 2

    def test_process_messages_advances_last_id(self):
        configure_registry()
        entries = [
            ("1-1", {"service": "s", "type": "t", "data": "{}", "user_id": "9", "source_uid": "src"}),
            ("1-2", {"service": "s", "type": "t", "data": "{}", "user_id": "9", "source_uid": "src"}),
        ]
        cm = WbConnectionManager(cache_db=self._fake_cache_db(entries))
        assert cm.stream_last_id == "$"
        asyncio.run(cm.process_messages())
        assert cm.stream_last_id == "1-2"
