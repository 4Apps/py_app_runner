import logging
import socket
import sys
import time
from argparse import Namespace
from difflib import SequenceMatcher
from typing import TypedDict

import sentry_sdk
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.tornado import TornadoIntegration
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.types import Event, Hint
from sentry_sdk.utils import AnnotatedValue

from py_app_runner.registry import AppRegistry

from .colors import Colors
from .http_exception import HTTPException
from .utils import is_sensitive_key

# Constants
ERROR_RATE = 60  # Minute


# Types
class LastError(TypedDict):
    count: int
    time: int
    msg: str


# Globals
last_error: LastError = {"count": 0, "time": 0, "msg": ""}
logger = logging.getLogger("meta")


#################
### Functions ###
#################


def RateControl(event: Event, hint: Hint) -> Event | None:
    if "exc_info" in hint:
        _exc_type, exc_value, _tb = hint["exc_info"]
        if isinstance(exc_value, HTTPException):
            return None

    if "extra" in event and "dispatch" in event["extra"] and not event["extra"]["dispatch"]:
        sys.stderr.write("#### Manually discarded error message meant to be sent via Sentry\n")
        return None

    now = int(time.time())
    current_event = str(event)
    test = SequenceMatcher(None, last_error["msg"], current_event).ratio()
    if last_error["time"] == 0 or now - int(last_error["time"]) >= ERROR_RATE or test < 0.4:
        sys.stderr.write("#### Allowing Sentry to send error message\n")
        last_error["count"] = 0
        last_error["time"] = now
        last_error["msg"] = current_event
        return event

    sys.stderr.write("#### Discarded error message meant to be sent to Sentry\n")
    last_error["count"] += 1
    return None


def InitSentry(
    args: Namespace,
    sentry_dns: str,
    release: str,
    environment: str,
    server_name: str | None = None,
) -> None:
    config = AppRegistry.config()
    if config.get("environment") != "prod":
        logger.debug(f"Sentry logging disabled in {config['environment']}")
        return

    logger.debug("Enabling Sentry logging")
    server_ip = socket.gethostbyname(socket.gethostname())
    scope = sentry_sdk.get_isolation_scope()
    scope.set_tag("server_ip", server_ip)
    scope.set_level(args.sv)

    # Tracing/profiling rates come from config when a project sets them; 0 disables.
    rates = (config.get("sentry") or {}).get("rate") or {}

    sentry_sdk.init(
        sentry_dns,
        release=release,
        environment=environment,
        server_name=server_name,
        attach_stacktrace=True,
        # The resolved config is a local in runner.main, so every stack trace would carry it.
        include_local_variables=False,
        event_scrubber=SensitiveKeyScrubber(recursive=True),
        before_send=RateControl,
        integrations=[TornadoIntegration(), RedisIntegration()],
        traces_sample_rate=float(rates.get("performance", 0) or 0),
        profiles_sample_rate=float(rates.get("profiles", 0) or 0),
        ignore_errors=[KeyboardInterrupt, HTTPException],
        # debug=True,
    )


###############
### Classes ###
###############


class SensitiveKeyScrubber(EventScrubber):
    """The SDK's denylist plus `is_sensitive_key`, since the stock match is by exact name and
    misses `db_password`. Keys are matched, never values: a password inside a URL still passes.

    Nested containers are scrubbed as copies - extras and breadcrumb data hold live references
    into the application's own objects.
    """

    def scrub_dict(self, d: object) -> None:
        if not isinstance(d, dict):
            return

        for k, v in d.items():
            if isinstance(k, str) and (k.lower() in self.denylist or is_sensitive_key(k)):
                d[k] = AnnotatedValue.substituted_because_contains_sensitive_data()
            elif self.recursive and isinstance(v, (dict, list, tuple)):
                d[k] = self._scrubbed_copy(v)

    def _scrubbed_copy(self, value: object) -> object:
        if isinstance(value, dict):
            copy = dict(value)
            self.scrub_dict(copy)
            return copy
        if isinstance(value, (list, tuple)):
            return [self._scrubbed_copy(item) for item in value]
        return value


class ConsoleHandler(logging.Handler):
    name_filter: str | None

    should_buffer: bool
    the_buffer: list[logging.LogRecord]

    def __init__(self, filter: str | None = None, level: int = logging.NOTSET):
        super().__init__(level)

        self.name_filter = filter
        self.should_buffer = False
        self.the_buffer = []

    def emit_buffer(self) -> None:
        self.should_buffer = False

        for record in self.the_buffer:
            self.emit(record)
        self.the_buffer = []

    def emit(self, record: logging.LogRecord) -> None:
        if self.name_filter is not None and not record.name.startswith(self.name_filter):
            return  # Skip logging for this module

        if self.should_buffer:
            self.the_buffer.append(record)
            return

        # Colors
        prepend = ""
        append = ""
        if record.levelno == logging.INFO:
            prepend = Colors.Yellow
            append = Colors.ColorOff
        elif record.levelno == logging.DEBUG:
            prepend = Colors.Cyan
            append = Colors.ColorOff
        elif record.levelno == logging.WARNING:
            prepend = Colors.Purple
            append = Colors.ColorOff
        elif record.levelno == logging.ERROR:
            prepend = Colors.Red
            append = Colors.ColorOff

        # End char for printing to console
        end: str = getattr(record, "end", "\n")
        prefix: str = getattr(record, "prefix", "")

        # Date
        date = ""
        clean = getattr(record, "clean", False)
        skip_date = getattr(record, "skip_date", False)
        if not clean:
            date = f"[{record.levelname}] [{record.name}] "
            if not skip_date:
                date = f"[{time.strftime('%d.%m.%Y %H:%M:%S')}] [{record.levelname}] [{record.name}] "

        # Message
        error_msg = f"{prepend}{date}{self.format(record)}{append}"

        # Print to console
        sys.stderr.write(f"{prefix}{error_msg}{end}")
