"""CLI subparsers for the built-in queue service.

    python3 src/app.py queue install [--dir PATH]
    python3 src/app.py queue work    [--queue a,b] [--once] [--stop-when-empty]
                                     [--max-jobs N] [--max-time N] [--timeout N] [--sleep N]
    python3 src/app.py queue status
    python3 src/app.py queue failed  [--limit N]
    python3 src/app.py queue retry   (--id N | --all)
    python3 src/app.py queue forget  (--id N | --all | --before DATE)

There is no --driver flag by design: the driver comes from config, so a push and a worker
cannot disagree about where the jobs are.
"""

import logging
from argparse import ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge


def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    _pybridge: PyBridge,
    _base_logger: logging.Logger,
) -> None:
    """Command line subparsers"""

    parser = subparsers.add_parser(
        "queue",
        description="Install the queue schema, run workers, and work through failed jobs",
        help="Job queue",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    install_parser = group.add_parser("install", help="Write the queue schema into the migrations directory")
    install_parser.add_argument("--dir", default=None, help="Migrations directory to write into")

    work_parser = group.add_parser("work", help="Reserve and run jobs until told to stop")
    work_parser.add_argument("--queue", default=None, help="Comma separated, in precedence order")
    work_parser.add_argument("--timeout", type=int, default=None, help="Visibility and per-job timeout")
    work_parser.add_argument("--sleep", type=float, default=None, help="Idle poll interval in seconds")
    work_parser.add_argument("--max-jobs", type=int, default=0, help="Stop after N jobs (0 = no limit)")
    work_parser.add_argument("--max-time", type=int, default=0, help="Stop after N seconds (0 = no limit)")
    work_parser.add_argument(
        "--stop-when-empty",
        action="store_true",
        help="Exit once the queue has nothing due, for a cron-driven worker",
    )
    work_parser.add_argument(
        "--once",
        action="store_true",
        help="Run at most one job and exit; implies --stop-when-empty",
    )

    group.add_parser("status", help="Show what is queued, delayed, held and failed")

    failed_parser = group.add_parser("failed", help="List failed jobs")
    failed_parser.add_argument("--limit", type=int, default=20, help="How many to show (default: 20)")

    retry_parser = group.add_parser("retry", help="Put failed jobs back on the queue")
    retry_parser.add_argument("--id", type=int, default=None, help="A single failed job id")
    retry_parser.add_argument("--all", action="store_true", help="Every failed job")

    forget_parser = group.add_parser("forget", help="Delete failed jobs without requeueing them")
    forget_parser.add_argument("--id", type=int, default=None, help="A single failed job id")
    forget_parser.add_argument("--all", action="store_true", help="Every failed job")
    forget_parser.add_argument("--before", default=None, help="Delete rows failed before this date")
