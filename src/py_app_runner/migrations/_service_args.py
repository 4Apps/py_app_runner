"""CLI subparsers for the built-in migrations service.

    python3 src/app.py migrations status   [--check] [--target NAME]
    python3 src/app.py migrations apply    [--dry-run] [--to PREFIX] [--target NAME]
    python3 src/app.py migrations baseline [--to PREFIX] [--yes] [--target NAME]
    python3 src/app.py migrations new      <name> [--target NAME]
    python3 src/app.py migrations repair   <filename> [--target NAME]

`--target` has no top-level counterpart on `runner.py`'s parser (unlike `apply`'s
`--dry-run`, which shadows a real top-level flag and needs `default=SUPPRESS` to avoid
clobbering it - see the collision test in tests/test_migrations_service.py). A plain
`default=None` is enough here, and `None` is what lets `init_service` tell "no --target
given" apart from "given as main".
"""

import logging
from argparse import SUPPRESS, ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge


def _add_target(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help='Named migrations target to operate on (see config["migrations"]["targets"])',
    )


def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    _pybridge: PyBridge,
    _base_logger: logging.Logger,
) -> None:
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
    _add_target(status_parser)

    apply_parser = group.add_parser("apply", help="Apply every pending migration, in order")
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=SUPPRESS,
        help="List what would run; change nothing",
    )
    apply_parser.add_argument(
        "--to",
        type=str,
        default=None,
        help="Stop after the migration with this YYYY-MM-DD-HHMMSS prefix",
    )
    _add_target(apply_parser)

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
    _add_target(baseline_parser)

    new_parser = group.add_parser("new", help="Create an empty, timestamped migration file")
    new_parser.add_argument("name", type=str, help="Short description, e.g. 'add widgets table'")
    _add_target(new_parser)

    repair_parser = group.add_parser("repair", help="Re-stamp one migration's checksum after a deliberate edit")
    repair_parser.add_argument("name", type=str, help="Migration filename, e.g. 2026-08-04-091530-a.sql")
    _add_target(repair_parser)
