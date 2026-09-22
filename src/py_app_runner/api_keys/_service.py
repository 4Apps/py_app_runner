import asyncio
import logging
from argparse import Namespace
from datetime import UTC, datetime

import psycopg

from py_app_runner.api_keys.commands import (
    Out,
    Update,
    UsageError,
    cmd_create,
    cmd_install,
    cmd_list,
    cmd_revoke,
    cmd_update,
    parse_abilities,
    parse_expires,
    parse_networks,
)
from py_app_runner.audit._service import _migrations_dir
from py_app_runner.migrations._service import connect_kwargs
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry


def _table() -> str:
    """The configured model's table, so an app that subclassed the model to rename it is
    managed where the bridge looks."""
    return getattr(AppRegistry.api_keys_model()(), "table_name", "api_keys")


async def _run(args: Namespace, config: dict, out: Out) -> int:
    table = _table()
    now = datetime.now(UTC)

    if args.step == "install":
        return cmd_install(_migrations_dir(args, config), table, now, out)

    db_config = (config.get("db") or {}).get("main")
    if not db_config:
        out('error: no database "main" in config["db"]')
        return 2

    change: Update | None = None
    abilities: list[str] = []
    allowed_ips: list[str] | None = None
    expires_at: datetime | None = None
    if args.step == "create":
        if not config.get("api_key_pepper"):
            out('error: config["api_key_pepper"] is not set; keys cannot be hashed')
            return 2
        abilities = parse_abilities(args.ability)
        allowed_ips = parse_networks(args.allowed_ip) if args.allowed_ip else None
        expires_at = parse_expires(args.expires, now) if args.expires else None
    elif args.step == "update":
        change = Update(
            name=args.name,
            abilities=parse_abilities(args.ability) if args.ability else None,
            add_abilities=parse_abilities(args.add_ability) or None,
            remove_abilities=parse_abilities(args.remove_ability) or None,
            allowed_ips=parse_networks(args.allowed_ip) if args.allowed_ip else None,
            any_ip=args.any_ip,
            expires_at=parse_expires(args.expires, now) if args.expires else None,
            no_expiry=args.no_expiry,
            user_id=args.user_id,
            no_user=args.no_user,
        )
        if change.is_empty():
            out("error: nothing to update; see --help")
            return 2
    elif args.step not in ("list", "revoke"):
        out(f"error: unknown api_keys command {args.step!r}")
        return 2

    async with await psycopg.AsyncConnection.connect(**connect_kwargs(db_config)) as conn:
        if args.step == "create":
            return await cmd_create(
                conn,
                table,
                config["api_key_pepper"],
                name=args.name,
                abilities=abilities,
                allowed_ips=allowed_ips,
                expires_at=expires_at,
                user_id=args.user_id,
                out=out,
            )
        if args.step == "list":
            return await cmd_list(conn, table, out)
        if args.step == "revoke":
            return await cmd_revoke(conn, table, args.ref, out)

        assert change is not None
        return await cmd_update(conn, table, args.ref, change, out)


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    # runner.py catches Exception around init_service and returns normally, which exits 0;
    # every outcome leaves through SystemExit so a failed revoke cannot report success.
    try:
        code = await _run(args, AppRegistry.config(), print)
    except UsageError as e:
        print(f"error: {e}")
        raise SystemExit(2) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(1) from None
    except Exception:
        logger.exception("api_keys: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)
