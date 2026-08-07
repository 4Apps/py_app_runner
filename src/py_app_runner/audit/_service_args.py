"""CLI subparsers for the built-in audit service.

    python3 src/app.py audit install [--dir PATH] [--table NAME]
    python3 src/app.py audit prune --before YYYY-MM-DD [--batch N] [--dry-run] [--db NAME]

`--dry-run` shadows a real top-level flag on runner.py's parser and needs
`default=SUPPRESS`, the same collision migrations' `apply` has.
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
        "audit",
        description="Install the audit trail schema and prune old rows",
        help="Audit trail",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    install_parser = group.add_parser("install", help="Write the audit schema into the migrations directory")
    install_parser.add_argument("--dir", default=None, help="Migrations directory to write into")
    install_parser.add_argument("--table", default=None, help="Audit table (default: from config)")

    prune_parser = group.add_parser("prune", help="Delete trail rows older than a date")
    prune_parser.add_argument("--before", required=True, help="Delete rows older than this, YYYY-MM-DD")
    prune_parser.add_argument("--batch", type=int, default=10000, help="Rows per statement (default: 10000)")
    prune_parser.add_argument("--table", default=None, help="Audit table (default: from config)")
    prune_parser.add_argument("--db", default=None, help='Entry of config["db"] to prune in (default: main)')
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=SUPPRESS,
        help="Count what would go, delete nothing",
    )
