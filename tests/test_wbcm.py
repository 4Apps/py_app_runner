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


class TestReAuthRebinding:
    def test_reassigning_user_drops_the_previous_binding(self):
        # A socket that re-authenticates as another user must not keep receiving the
        # previous user's messages.
        uc = UserConnections()
        conn = FakeConn("a")
        uc.add_connection(conn)
        uc.assign_user_id("a", "1")
        uc.assign_user_id("a", "2")

        assert uc.get_connections_by_user_id("1") == []
        assert uc.get_connections_by_user_id("2") == [conn]
        assert "1" not in uc.user_id_map

    def test_unassign_connection_clears_all_bindings(self):
        uc = UserConnections()
        conn = FakeConn("a")
        uc.add_connection(conn)
        uc.assign_user_id("a", "1")
        uc.unassign_connection("a")

        assert uc.user_id_map == {}
        assert "a" in uc.user_connections

    def test_remove_connection_leaves_no_stale_entries(self):
        uc = UserConnections()
        conn_a = FakeConn("a")
        conn_b = FakeConn("b")
        for conn in (conn_a, conn_b):
            uc.add_connection(conn)
        uc.assign_user_id("a", "1")
        uc.assign_user_id("b", "1")
        uc.assign_user_id("a", "2")

        uc.remove_connection(conn_a)
        assert uc.user_id_map == {"1": ["b"]}

    def test_device_reassignment_drops_previous_binding(self):
        dc = DeviceConnections()
        conn = FakeConn("a")
        dc.add_connection(conn)
        dc.assign_device_id("a", "dev1")
        dc.assign_device_id("a", "dev2")

        assert dc.get_connections_by_device_id("dev1") == []
        assert dc.get_connections_by_device_id("dev2") == [conn]


class BrokenLoop:
    def call_soon_threadsafe(self, fn, *args):
        raise RuntimeError("loop is closed")


class TestFanOutResilience:
    def test_one_dead_connection_does_not_stop_delivery(self):
        cm = make_manager()
        dead = FakeConn("dead")
        dead.loop = BrokenLoop()  # type: ignore[assignment]
        alive = FakeConn("alive")
        for conn in (dead, alive):
            cm.user_connections.add_connection(conn)
            cm.user_connections.assign_user_id(conn.uid, "1")

        cm.send_message("1", {"hello": "world"}, "src")

        assert alive.messages == [{"hello": "world"}]
        assert "dead" not in cm.user_connections.user_connections

    def test_send_to_all_survives_removal_during_iteration(self):
        cm = make_manager()
        dead = FakeConn("dead")
        dead.loop = BrokenLoop()  # type: ignore[assignment]
        alive = FakeConn("alive")
        for conn in (dead, alive):
            cm.user_connections.add_connection(conn)

        cm.send_message_to_all({"hello": "all"}, "src")

        assert alive.messages == [{"hello": "all"}]
        assert "dead" not in cm.user_connections.user_connections

    def test_device_fan_out_continues_past_dead_connection(self):
        cm = make_manager()
        dead = FakeConn("dead")
        dead.loop = BrokenLoop()  # type: ignore[assignment]
        alive = FakeConn("alive")
        for conn in (dead, alive):
            cm.device_connections.add_connection(conn)
            cm.device_connections.assign_device_id(conn.uid, "dev1")

        cm.send_message_to_device("dev1", {"ping": 1}, "src")

        assert alive.messages == [{"ping": 1}]


class TestPoisonMessages:
    def test_undecodable_data_is_dropped_not_raised(self):
        cm = make_manager()
        message = {
            "service": "s",
            "type": "t",
            "data": "{not json",
            "user_id": "1",
            "source_uid": "src",
        }
        assert cm.work(message) is False

    def test_failing_message_still_advances_the_stream(self):
        # Otherwise the same batch is re-read on every tick forever.
        configure_registry()
        entries = [
            ("1-1", {"service": "s", "type": "t", "data": "{}", "user_id": "1", "source_uid": "src"}),
            ("1-2", {"service": "s", "type": "t", "data": "{}", "user_id": "1", "source_uid": "src"}),
        ]
        cache_db = MagicMock()
        cache_db.connection.xread = AsyncMock(return_value={TEST_STREAM: [entries]})
        cm = WbConnectionManager(cache_db=cache_db)

        def explode(message_data: Any) -> bool:
            raise RuntimeError("boom")

        cm.work = explode  # type: ignore[assignment]
        asyncio.run(cm.process_messages())

        assert cm.stream_last_id == "1-2"


class TestShutdown:
    def test_shutdown_closes_sockets_on_their_own_loop(self):
        # Sockets belong to the web server's loop; the manager runs on a background
        # thread and must hand the close over rather than calling it directly.
        cm = make_manager()
        scheduled: list[Any] = []

        class RecordingLoop:
            def call_soon_threadsafe(self, fn, *args):
                scheduled.append(fn)
                fn(*args)

        conn = FakeConn("a")
        conn.loop = RecordingLoop()  # type: ignore[assignment]
        cm.user_connections.add_connection(conn)
        cm.user_connections.assign_user_id("a", "1")

        asyncio.run(cm.shutdown())

        assert scheduled == [conn.close]
        assert conn.closed
        assert cm.user_connections.user_connections == {}
