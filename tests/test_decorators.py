import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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


class FakePipeline:
    def __init__(self, store: dict[str, int], ttls: dict[str, int]):
        self.store = store
        self.ttls = ttls
        self.ops: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def incr(self, key: str):
        self.ops.append(("incr", key))
        return self

    def ttl(self, key: str):
        self.ops.append(("ttl", key))
        return self

    async def execute(self) -> list[int]:
        results: list[int] = []
        for op, key in self.ops:
            if op == "incr":
                self.store[key] = self.store.get(key, 0) + 1
                results.append(self.store[key])
            else:
                results.append(self.ttls.get(key, -1))
        self.ops = []
        return results


class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self.store, self.ttls)

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        self.expire_calls.append((key, seconds))
        return True


class RateLimitedHandler:
    def __init__(self, redis_con: FakeRedis):
        self.redis_con = redis_con
        self.bridge_handler = MagicMock()
        self.bridge_handler.current_user = MagicMock(id=7)

    @rate_limit(2, 60)
    async def limited(self, input_data: dict[str, Any]) -> str:
        return "ok"


class TestRateLimit:
    def test_allows_up_to_the_limit_then_raises(self):
        redis_con = FakeRedis()
        handler = RateLimitedHandler(redis_con)

        assert asyncio.run(handler.limited({})) == "ok"
        assert asyncio.run(handler.limited({})) == "ok"

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(handler.limited({}))
        assert excinfo.value.http_status == 429

    def test_ttl_is_rearmed_when_missing(self):
        # A crash between INCR and EXPIRE leaves a key with no TTL; without repair
        # the identity stays locked out forever.
        redis_con = FakeRedis()
        handler = RateLimitedHandler(redis_con)
        asyncio.run(handler.limited({}))

        key = redis_con.expire_calls[0][0]
        del redis_con.ttls[key]

        asyncio.run(handler.limited({}))
        assert redis_con.expire_calls[-1] == (key, 60)

    def test_key_is_scoped_per_handler_class(self):
        redis_con = FakeRedis()

        class OtherHandler(RateLimitedHandler):
            pass

        asyncio.run(RateLimitedHandler(redis_con).limited({}))
        asyncio.run(OtherHandler(redis_con).limited({}))

        assert len(redis_con.store) == 2
