"""CLI subparsers for the built-in migrations service.

    python3 src/app.py migrations status   [--check]
    python3 src/app.py migrations apply    [--dry-run] [--to PREFIX]
    python3 src/app.py migrations baseline [--to PREFIX] [--yes]
    python3 src/app.py migrations new      <name>
    python3 src/app.py migrations repair   <filename>
"""

import logging
from argparse import ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge


def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    _pybridge: PyBridge,
    _base_logger: logging.Logger,
):
    """Command line subparsers"""

    parser = subparsers.add_parser(
        "migrations",
        description="Apply tracked SQL migrations to the main database",
        help="Database migrations",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    status_parser = group.add_parser("status", help="Show which migrations are applied and which are pending")
    status_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if anything is pending or drifted (for deploy assertions)",
    )

    apply_parser = group.add_parser("apply", help="Apply every pending migration, in order")
    apply_parser.add_argument("--dry-run", action="store_true", help="List what would run; change nothing")
    apply_parser.add_argument(
        "--to",
        type=str,
        default=None,
        help="Stop after the migration with this YYYY-MM-DD-HHMMSS prefix",
    )

    baseline_parser = group.add_parser(
        "baseline",
        help="Record migrations as applied WITHOUT running them (adopting an existing database)",
    )
    baseline_parser.add_argument(
        "--to",
        type=str,
        default=None,
        help="Only offer migrations up to and including this YYYY-MM-DD-HHMMSS prefix",
    )
    baseline_parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask; stamp every offered migration (for scripts)",
    )

    new_parser = group.add_parser("new", help="Create an empty, timestamped migration file")
    new_parser.add_argument("name", type=str, help="Short description, e.g. 'add widgets table'")

    repair_parser = group.add_parser("repair", help="Re-stamp one migration's checksum after a deliberate edit")
    repair_parser.add_argument("name", type=str, help="Migration filename, e.g. 2026-08-04-091530-a.sql")
