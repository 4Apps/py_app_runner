import json
import logging
from argparse import Namespace
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

import py_app_runner.logger_handlers as lh
from py_app_runner.logger_handlers import ConsoleHandler, InitSentry, RateControl
from py_app_runner.registry import AppRegistry

SENTINEL = "s3ntinel-value"


def reset_rate_state() -> None:
    lh.last_error["count"] = 0
    lh.last_error["time"] = 0
    lh.last_error["msg"] = ""


class TestRateControl:
    def test_duplicate_event_is_suppressed(self):
        # The similarity check compares two strings; comparing a one element list
        # against a string scored 0.0 for everything and disabled the rate limit.
        reset_rate_state()
        event = {"message": "ValueError at foo.py line 10"}

        assert RateControl(dict(event), {}) is not None
        assert RateControl(dict(event), {}) is None
        assert lh.last_error["count"] == 1

    def test_unrelated_event_still_gets_through(self):
        reset_rate_state()

        assert RateControl({"message": "ValueError at foo.py line 10"}, {}) is not None
        assert RateControl({"message": "completely different subsystem failure"}, {}) is not None


class TestConsoleHandler:
    def test_buffer_is_per_instance(self):
        a = ConsoleHandler()
        b = ConsoleHandler()
        a.should_buffer = True
        a.the_buffer.append("record")  # type: ignore[arg-type]

        assert b.the_buffer == []


class CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.events.extend(item.payload.json for item in envelope.items if item.type == "event")


@pytest.fixture
def sentry_events(monkeypatch):
    """Initialises Sentry exactly as a production service would, minus the network."""
    reset_rate_state()
    monkeypatch.setattr(AppRegistry, "_config", {"environment": "prod"})
    InitSentry(Namespace(sv="error"), "https://public@sentry.invalid/1", "test", "prod")
    transport = CapturingTransport()
    sentry_sdk.get_client().transport = transport
    yield transport.events
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)


def nested_config() -> dict[str, Any]:
    return {
        "db": {"main": {"hostname": "db1", "port": 5432, "database": "app", "username": SENTINEL}},
        "oauth": [{"client_id": "abc", "azure_client_secret": SENTINEL}],
        "POSTGRES_PASSWORD": SENTINEL,
        "pg_pass": SENTINEL,
        "sentry": {"dsn": SENTINEL},
        "user_id": 7,
        "user_agent": "curl",
        "sass_file": "site.scss",
    }


class TestSentryScrubbing:
    def test_exception_event_carries_no_frame_locals(self, sentry_events):
        config = nested_config()  # noqa: F841 - the local the SDK would otherwise snapshot
        try:
            raise ValueError("boom")
        except ValueError:
            sentry_sdk.capture_exception()

        assert len(sentry_events) == 1
        assert SENTINEL not in json.dumps(sentry_events[0])

    def test_logged_extras_are_masked_at_any_depth_without_touching_the_original(self, sentry_events):
        config = nested_config()
        logging.getLogger("test_sentry").error("failed", extra={"config": config})

        assert len(sentry_events) == 1
        sent = sentry_events[0]["extra"]["config"]
        assert SENTINEL not in json.dumps(sent)
        assert sent["db"]["main"] == {"hostname": "db1", "port": 5432, "database": "app", "username": "[Filtered]"}
        assert sent["oauth"][0]["client_id"] == "abc"
        assert (sent["user_id"], sent["user_agent"], sent["sass_file"]) == (7, "curl", "site.scss")
        assert config == nested_config()
