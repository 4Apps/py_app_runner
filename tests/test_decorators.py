import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from py_app_runner.http_exception import HTTPException
from py_app_runner.request_handler.decorators import (
    _sanitize_input,
    action,
    audited,
    authenticated,
    rate_limit,
    require_auth_for_actions,
)
from py_app_runner.request_handler.handlers import RequestHandlerHelper
from tests.redis_harness import redis_for


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


class TestRequireAuthForActions:
    def test_inherited_actions_are_guarded(self):
        # Dispatch resolves actions through dir(), so an @action defined on a base
        # class is routable and must be wrapped too.
        class Base:
            @action("inherited")
            async def inherited(self, input_data: dict[str, Any]) -> str:
                return "reached handler"

        @require_auth_for_actions
        class Child(Base):
            @action("own")
            async def own(self, input_data: dict[str, Any]) -> str:
                return "reached handler"

        handler = Child()
        handler.bridge_handler = MagicMock()
        handler.bridge_handler.current_user = None

        for attr in (handler.inherited, handler.own):
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(attr({}))
            assert excinfo.value.http_status == 401

    def test_already_authenticated_action_is_not_double_wrapped(self):
        calls: list[str] = []

        @require_auth_for_actions
        class Handler:
            @action("guarded")
            @authenticated
            async def guarded(self, input_data: dict[str, Any]) -> str:
                calls.append("ran")
                return "ok"

        handler = Handler()
        handler.bridge_handler = MagicMock()
        handler.bridge_handler.current_user = MagicMock(id=1)

        assert asyncio.run(handler.guarded({})) == "ok"
        assert calls == ["ran"]

    def test_actions_still_dispatchable_after_wrapping(self):
        @require_auth_for_actions
        class Handler(RequestHandlerHelper):
            @action("ping")
            async def ping(self, input_data: dict[str, Any]) -> str:
                return "pong"

        assert "ping" in Handler._get_actions()


class RateLimitedHandler:
    def __init__(self, redis_con: Any):
        self.redis_con = redis_con
        self.bridge_handler = MagicMock()
        self.bridge_handler.current_user = MagicMock(id=7)

    @rate_limit(2, 60)
    async def limited(self, input_data: dict[str, Any]) -> str:
        return "ok"


class TestRateLimit:
    """Counting moved into py_app_runner.throttle, which is covered in depth by
    test_throttle.py. What is left to prove here is the decorator's own job: deriving the
    key, and turning a denial into a 429.

    These run against a real Redis rather than a fake, because the counting is now a Lua
    script and a fake would be asserting against a reimplementation of it.
    """

    @pytest_asyncio.fixture
    async def redis_con(self):
        async with redis_for(database=3) as con:
            yield con

    async def test_allows_up_to_the_limit_then_raises(self, redis_con):
        handler = RateLimitedHandler(redis_con)

        assert await handler.limited({}) == "ok"
        assert await handler.limited({}) == "ok"

        with pytest.raises(HTTPException) as excinfo:
            await handler.limited({})
        assert excinfo.value.http_status == 429

    async def test_the_denial_says_when_to_come_back(self, redis_con):
        handler = RateLimitedHandler(redis_con)
        await handler.limited({})
        await handler.limited({})

        with pytest.raises(HTTPException) as excinfo:
            await handler.limited({})
        assert "seconds" in str(excinfo.value.message)

    async def test_key_is_scoped_per_handler_class(self, redis_con):
        """The same action name in two services would otherwise share a single bucket."""

        class OtherHandler(RateLimitedHandler):
            pass

        await RateLimitedHandler(redis_con).limited({})
        await OtherHandler(redis_con).limited({})

        assert len(await redis_con.keys("*")) == 2

    async def test_key_is_scoped_per_identity(self, redis_con):
        first = RateLimitedHandler(redis_con)
        second = RateLimitedHandler(redis_con)
        second.bridge_handler.current_user = MagicMock(id=8)

        await first.limited({})
        await second.limited({})

        assert len(await redis_con.keys("*")) == 2

    async def test_without_redis_it_warns_and_lets_the_call_through(self):
        """Enforcement is best-effort by design: a cache outage must not take the
        application down with it."""

        handler = RateLimitedHandler(None)
        handler.redis_con = None

        assert await handler.limited({}) == "ok"
