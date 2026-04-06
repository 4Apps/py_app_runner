import logging
import socket
import sys
import time
from argparse import Namespace
from difflib import SequenceMatcher
from typing import Any, TypedDict

import sentry_sdk
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.tornado import TornadoIntegration

from py_app_runner.registry import AppRegistry

from .colors import Colors
from .http_exception import HTTPException

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


def RateControl(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    if "exc_info" in hint:
        _exc_type, exc_value, _tb = hint["exc_info"]
        if isinstance(exc_value, HTTPException):
            return None

    if "extra" in event and "dispatch" in event["extra"] and not event["extra"]["dispatch"]:
        sys.stderr.write("#### Manually discarded error message meant to be sent via Sentry\n")
        return None

    now = int(time.time())
    current_event = str(event)
    test = SequenceMatcher(None, [last_error["msg"]], current_event).ratio()
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
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("server_ip", server_ip)
        scope.level = args.sv  # pyrefly: ignore[missing-attribute]

    sentry_sdk.init(
        sentry_dns,
        release=release,
        environment=environment,
        server_name=server_name,
        attach_stacktrace=True,
        before_send=RateControl,  # type: ignore
        integrations=[TornadoIntegration(), RedisIntegration()],
        # traces_sample_rate=float(config["sentry"]["rate"]["performance"]),
        # profiles_sample_rate=float(config["sentry"]["rate"]["profiles"]),
        ignore_errors=[KeyboardInterrupt, HTTPException],
        # debug=True,
    )


###############
### Classes ###
###############


class ConsoleHandler(logging.Handler):
    name_filter: str | None

    def __init__(self, filter: str | None = None, level: int = logging.NOTSET):
        super().__init__(level)

        self.name_filter = filter

    should_buffer: bool = False
    the_buffer: list[logging.LogRecord] = []

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
