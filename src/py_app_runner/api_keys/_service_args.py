"""CLI subparsers for the built-in api_keys service.

python3 src/app.py api_keys install [--dir PATH]
python3 src/app.py api_keys create --name NAME --ability A [--ability ...] [--allowed-ip CIDR ...]
                                   [--expires DATE] [--user-id N]
python3 src/app.py api_keys list
python3 src/app.py api_keys revoke <id|prefix>
python3 src/app.py api_keys update <id|prefix> [--name N] [--ability A ...] [--add-ability A ...]
                                   [--remove-ability A ...] [--allowed-ip CIDR ... | --any-ip]
                                   [--expires DATE | --no-expiry] [--user-id N | --no-user]
"""

import logging
from argparse import ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge

_ABILITY_HELP = "'service:action', 'service:*' or '*'; repeatable"
_EXPIRES_HELP = "YYYY-MM-DD (valid through that UTC day) or an ISO timestamp"


def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    _pybridge: PyBridge,
    _base_logger: logging.Logger,
) -> None:
    """Command line subparsers"""

    parser = subparsers.add_parser(
        "api_keys",
        description="Create, list, revoke and update scoped API keys",
        help="API keys",
    )
    group = parser.add_subparsers(title="command", dest="step", required=True)

    install_parser = group.add_parser("install", help="Write the api_keys schema into the migrations directory")
    install_parser.add_argument("--dir", default=None, help="Migrations directory to write into")

    create_parser = group.add_parser("create", help="Create a key and print it once")
    create_parser.add_argument("--name", required=True, help="Who or what the key is for")
    create_parser.add_argument("--ability", action="append", required=True, help=_ABILITY_HELP)
    create_parser.add_argument("--allowed-ip", action="append", help="IP or CIDR; repeatable (default: any)")
    create_parser.add_argument("--expires", default=None, help=_EXPIRES_HELP)
    create_parser.add_argument("--user-id", type=int, default=None, help="User the key acts as without a JWT")

    group.add_parser("list", help="List keys; secrets are never shown")

    revoke_parser = group.add_parser("revoke", help="Disable a key")
    revoke_parser.add_argument("ref", help="Key id or prefix")

    update_parser = group.add_parser("update", help="Change a key's name, abilities, IPs, expiry or user")
    update_parser.add_argument("ref", help="Key id or prefix")
    update_parser.add_argument("--name", default=None)
    update_parser.add_argument("--ability", action="append", help=f"Replace all abilities: {_ABILITY_HELP}")
    update_parser.add_argument("--add-ability", action="append", help="Grant an ability; repeatable")
    update_parser.add_argument("--remove-ability", action="append", help="Withdraw an ability; repeatable")
    ips = update_parser.add_mutually_exclusive_group()
    ips.add_argument("--allowed-ip", action="append", help="Replace the allowed IPs; repeatable")
    ips.add_argument("--any-ip", action="store_true", help="Allow any address")
    expiry = update_parser.add_mutually_exclusive_group()
    expiry.add_argument("--expires", default=None, help=_EXPIRES_HELP)
    expiry.add_argument("--no-expiry", action="store_true", help="Never expire")
    user = update_parser.add_mutually_exclusive_group()
    user.add_argument("--user-id", type=int, default=None, help="User the key acts as without a JWT")
    user.add_argument("--no-user", action="store_true", help="Act as no user")
