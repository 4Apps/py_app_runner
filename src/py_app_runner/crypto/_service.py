import asyncio
import logging
from argparse import Namespace

import psycopg

from py_app_runner.crypto.commands import Out, cmd_key, cmd_rotate
from py_app_runner.crypto.errors import CryptoError
from py_app_runner.crypto.fields import FieldCrypto
from py_app_runner.migrations._service import connect_kwargs
from py_app_runner.pybridge import PyBridge
from py_app_runner.registry import AppRegistry

_DEFAULT_DB = "main"


async def init_service(args: Namespace, _pybridge: PyBridge, logger: logging.Logger) -> None:
    out: Out = print

    # `key` runs before any config is read and any connection is opened. Generating the
    # first key must not require a working install, because until it exists there isn't
    # one - the environment variable it goes into is what the config refers to.
    if args.step == "key":
        raise SystemExit(cmd_key(out))

    # Same reasoning as migrations: runner.py catches Exception around init_service and
    # returns normally, which exits 0. A rotate that could not reach the database must not
    # report success to a deploy script, so every non-SystemExit failure becomes a non-zero
    # exit here rather than in runner.py.
    code = 1
    try:
        config = AppRegistry.config()
        crypto = FieldCrypto.from_config(config)

        db_name = getattr(args, "db", None) or _DEFAULT_DB
        db_config = config.get("db") or {}
        if db_name not in db_config:
            out(
                f'error: no database {db_name!r} in config["db"]; '
                f"configured are: {', '.join(sorted(db_config)) or 'none'}."
            )
            raise SystemExit(2)

        if args.step == "rotate":
            async with await psycopg.AsyncConnection.connect(**connect_kwargs(db_config[db_name])) as conn:
                code = await cmd_rotate(
                    conn,
                    crypto,
                    args.table,
                    args.column,
                    args.id,
                    args.batch,
                    getattr(args, "dry_run", False),
                    out,
                )
        else:
            out(f"error: unknown crypto command {args.step!r}")
            code = 1

    except CryptoError as e:
        # Configuration and key material problems are the expected failure here, and their
        # messages already say what to do. A stack trace would bury that.
        out(f"error: {e}")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Above `except Exception` because both derive from BaseException. An interrupted
        # rotate has re-encrypted some rows and not others, which is safe to resume but must
        # not be reported as done.
        logger.error("crypto: interrupted; the column may be partially rotated")
        raise SystemExit(1) from None
    except Exception:
        logger.exception("crypto: unhandled failure")
        raise SystemExit(1) from None

    raise SystemExit(code)
