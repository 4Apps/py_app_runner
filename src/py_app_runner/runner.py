#!/usr/bin/env python3
# ruff: noqa: E402 # Ignore module level import not at top of file

import argparse
import asyncio
import logging
import os
import sys

import uvloop

from py_app_runner.config import is_env_dev, is_env_prod
from py_app_runner.logger_handlers import ConsoleHandler, InitSentry
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry
from py_app_runner.utils import json_encode


def main() -> None:
    """Service initialization"""

    # Ensure src/ is on the path so project modules (config, services, etc.) are importable
    cwd = os.getcwd()
    src_path = os.path.join(cwd, "src")
    if os.path.isdir(src_path) and src_path not in sys.path:
        sys.path.insert(0, src_path)

    if not os.path.isfile(os.path.join(cwd, ".env")):
        raise FileNotFoundError("There is no .env file, exiting...")

    config = AppRegistry.config()

    # Parse commandline arguments
    parser = argparse.ArgumentParser(description="PyBridge services")
    parser.add_argument(
        "-v",
        type=str,
        choices=["debug", "info", "warning", "error", "disable"],
        default="info",
        help="Console logging level",
    )
    parser.add_argument(
        "-sv",
        type=str,
        choices=["debug", "info", "warning", "error", "disable"],
        default="error",
        help=(
            "Sentry logging level, depends on -v. If -v is set to disable, then no error will be sent, "
            "also if -v is set to info, and this is set to info, only errors will be reported."
        ),
    )
    parser.add_argument(
        "-vf",
        type=str,
        default=None,
        help="Console logging filter by module name",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="""
            Interval in seconds. Used in services that supports it.
            A service should chose their own defaults.
        """,
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run job only once",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode, no data will be saved, if subservice supports it",
    )

    # Parse args for logging output first
    args, _unknown = parser.parse_known_args()

    # Init logging
    consoleHandler = ConsoleHandler(filter=args.vf)
    logger = logging.getLogger()
    logger.addHandler(consoleHandler)

    consoleLogger = logging.getLogger("console")
    consoleLogger.addHandler(consoleHandler)
    consoleLogger.propagate = False

    if args.v == "disable":
        logging.disable(logging.CRITICAL)
    else:
        logger.setLevel(getattr(logging, args.v.upper()))
        consoleLogger.setLevel(getattr(logging, args.v.upper()))

    config["debug"] = args.v.upper() == "DEBUG"

    # Init sentry
    if is_env_prod(config):
        InitSentry(
            args,
            config["sentry"]["dsn"],
            config["git_commit_hash"],
            config["environment"],
        )

    if is_env_dev(config):
        # Enable debug mode for asyncio in development environment
        os.environ["PYTHONASYNCIODEBUG"] = "1"
    else:
        # Remove debug mode for asyncio in production and test environments
        os.environ.pop("PYTHONASYNCIODEBUG", None)
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    # Looking for service argument parsers...
    pybridge = PyBridge()
    subparsers = parser.add_subparsers(
        title="service",
        description="Type of service",
        help="Type of service",
        dest="service",
    )
    subparsers.add_parser("print_config", help="Prints the config")

    logger.debug("Looking for services subparsers...", extra={"prefix": "\n\n"})
    for item in config["services"]:
        if item == "bridge":
            from py_app_runner.bridge import _service_args as bridge_service_args

            service = bridge_service_args
        else:
            service = pybridge.load_service_args(item)

        logBuffer = f"{item}:"

        # Register subparser
        logBuffer += " subparser: "
        if service and hasattr(service, "reg_subparsers"):
            service.reg_subparsers(subparsers, pybridge, logger)
            logBuffer += "Yes"
        else:
            logBuffer += "No"

        # Log info
        logger.debug(logBuffer)

    # Second parse args for services
    args = parser.parse_args()

    # Start the service...
    if args.service is None:
        parser.print_help()
        return

    if args.service == "print_config":
        logger.info("Config dump:")
        print(json_encode(config, pretty=True))
        return

    logger.debug(f"\n\nLoad service: {args.service}")

    if args.service == "bridge":
        from py_app_runner.bridge import _service as bridge_service

        service = bridge_service
    else:
        service = pybridge.load_service_runner(args.service)

    if not service:
        logger.error(f'Service "{args.service}" not found!')
        return

    if not hasattr(service, "init_service"):
        logger.error(f'"init_service" method not found on {args.service}!')
        return

    logger.debug(f'Starting service: "{args.service}"', extra={"prefix": "\n\n"})

    # Start the service
    try:
        logger.info(f'Started service "{args.service}"')
        asyncio.run(service.init_service(args, pybridge, logger))

    except (KeyboardInterrupt, asyncio.CancelledError):
        ...  # Ignore this exception

    except Exception as e:
        logger.exception(f'Uncaught service "{args.service}" exception: {repr(e)}')
