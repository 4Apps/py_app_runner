"""CLI subparsers for the built-in crypto service.

    python3 src/app.py crypto key
    python3 src/app.py crypto rotate --table NAME --column NAME [--id NAME]
                                     [--batch N] [--dry-run] [--db NAME]

`--dry-run` shadows a real top-level flag on runner.py's parser, so it needs
`default=SUPPRESS` or argparse copies the subparser default back over the parent
namespace - the same collision migrations' `apply` has.
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
        "crypto",
        description="Generate key material and re-encrypt stored columns",
        help="Field encryption",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    group.add_parser("key", help="Print fresh key material for an environment variable")

    rotate_parser = group.add_parser("rotate", help="Re-encrypt a column under the current key")
    rotate_parser.add_argument("--table", required=True, help="Table holding the column")
    rotate_parser.add_argument("--column", required=True, help="Encrypted column")
    rotate_parser.add_argument("--id", default="id", help="Primary key to page through (default: id)")
    rotate_parser.add_argument(
        "--batch",
        type=int,
        default=500,
        help="Rows read per statement (default: 500)",
    )
    rotate_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=SUPPRESS,
        help="Report what would change, change nothing",
    )
    rotate_parser.add_argument(
        "--db",
        default=None,
        help='Entry of config["db"] to rotate in (default: main)',
    )
