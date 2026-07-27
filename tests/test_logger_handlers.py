import py_app_runner.logger_handlers as lh
from py_app_runner.logger_handlers import ConsoleHandler, RateControl


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
