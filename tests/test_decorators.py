import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from py_app_runner.request_handler.decorators import _sanitize_input, audited


class TestSanitizeInput:
    def test_masks_sensitive_fields(self):
        data = {"email": "a@b.c", "password": "hunter2"}
        assert _sanitize_input(data, ("password",)) == {"email": "a@b.c", "password": "***"}

    def test_non_dict_input_returns_empty(self):
        assert _sanitize_input("not a dict", ("password",)) == {}
        assert _sanitize_input(None, ("password",)) == {}


class FakeHandler:
    def __init__(self):
        self.db_wrapper = MagicMock()
        self.bridge_handler = MagicMock()
        self.bridge_handler.current_user = MagicMock(id=42)
        self.bridge_handler.request.remote_ip = "10.0.0.1"
        self.bridge_handler.request.headers = {"User-Agent": "test-agent"}


class TestAudited:
    def test_sink_receives_sanitized_record(self):
        sink = AsyncMock()

        @audited("users.create", sink=sink)
        async def create(self, input_data: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        handler = FakeHandler()
        result = asyncio.run(create(handler, {"email": "a@b.c", "password": "x"}))

        assert result == {"ok": True}
        sink.assert_awaited_once()
        kwargs = sink.await_args.kwargs
        assert kwargs["event_type"] == "users.create"
        assert kwargs["user_id"] == 42
        assert kwargs["ip"] == "10.0.0.1"
        assert kwargs["details"] == {"email": "a@b.c", "password": "***"}

    def test_sink_failure_does_not_fail_action(self):
        sink = AsyncMock(side_effect=RuntimeError("audit db down"))

        @audited(sink=sink)
        async def delete(self, input_data: dict[str, Any]) -> str:
            return "deleted"

        assert asyncio.run(delete(FakeHandler(), {})) == "deleted"

    def test_without_sink_logs_only(self):
        @audited()
        async def rename(self, input_data: dict[str, Any]) -> str:
            return "renamed"

        assert asyncio.run(rename(FakeHandler(), {"name": "x"})) == "renamed"
