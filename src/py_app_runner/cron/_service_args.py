"""CLI subparsers for the built-in cron service.

    python3 src/app.py cron list
    python3 src/app.py cron run  [--job NAME] [--dry-run]
    python3 src/app.py cron work

`run` is what the system crontab calls once a minute; `work` is the same thing in a loop
for a container with no crontab around it.

`--dry-run` shadows a real top-level flag and needs `default=SUPPRESS` so that the
subparser's default cannot overwrite a value the top-level parser already set.
"""

import logging
from argparse import SUPPRESS, ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge


def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    _pybridge: PyBridge,
    _base_logger: logging.Logger,
) -> None:
    """Command line subparsers"""

    parser = subparsers.add_parser(
        "cron",
        description="Run app.py subcommands on a schedule declared in config",
        help="Scheduled jobs",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    group.add_parser("list", help="Show every job with its schedule and next run")

    run_parser = group.add_parser("run", help="Run whatever is due this minute, then exit")
    run_parser.add_argument("--job", default=None, help="Run this one job now, whatever its schedule")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=SUPPRESS,
        help="Print what would run; start nothing",
    )

    group.add_parser("work", help="Run due jobs every minute until stopped")
