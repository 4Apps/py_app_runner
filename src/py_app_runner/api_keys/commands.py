"""Command implementations, importable directly for programmatic use.

Every command that writes takes an autocommit connection and opens its own transaction.
"""

import datetime
import pathlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from py_app_runner.api_keys.abilities import normalize_ability
from py_app_runner.api_keys.keys import expired, generate_key, hash_secret, parse_network
from py_app_runner.migrations.discovery import MigrationError, new_filename

Out = Callable[[str], None]

_TEMPLATE = pathlib.Path(__file__).parent / "files" / "install.pgsql.sql"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UsageError(ValueError):
    """Bad input on the command line; exits 2."""


@dataclass
class Update:
    """What `update` changes. None leaves a column alone; `any_ip` and `no_*` set it to NULL."""

    name: str | None = None
    abilities: list[str] | None = None
    add_abilities: list[str] | None = None
    remove_abilities: list[str] | None = None
    allowed_ips: list[str] | None = None
    any_ip: bool = False
    expires_at: datetime.datetime | None = None
    no_expiry: bool = False
    user_id: int | None = None
    no_user: bool = False

    def is_empty(self) -> bool:
        return self == Update()


def parse_abilities(raw: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for item in raw or ():
        try:
            ability = normalize_ability(item)
        except ValueError as e:
            raise UsageError(str(e)) from None
        if ability not in result:
            result.append(ability)

    return result


def parse_networks(raw: Iterable[str] | None) -> list[str]:
    try:
        return [parse_network(item) for item in raw or ()]
    except ValueError as e:
        raise UsageError(str(e)) from None


def parse_expires(raw: str, now: datetime.datetime) -> datetime.datetime:
    """A bare date means the key works through that UTC day; anything else is an ISO
    timestamp, UTC unless it carries an offset."""

    text = raw.strip()
    try:
        if _DATE_RE.match(text):
            day = datetime.date.fromisoformat(text)
            value = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(), datetime.UTC)
        else:
            value = datetime.datetime.fromisoformat(text)
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.UTC)
    except ValueError:
        raise UsageError(f"--expires={raw!r} must be YYYY-MM-DD or an ISO timestamp") from None

    if value <= now:
        raise UsageError(f"--expires={raw!r} is already in the past")

    return value


def _where_ref(ref: str) -> tuple[sql.Composable, Any]:
    # A pasted full key is accepted and only its prefix is used.
    ref = ref.strip().split(".", 1)[0]
    if ref.isdigit():
        return sql.SQL("id = %s"), int(ref)

    return sql.SQL("key_prefix = %s"), ref


def _when(value: datetime.datetime | None, default: str) -> str:
    if value is None:
        return default

    return value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _describe(row: dict[str, Any], out: Out) -> None:
    ips = row["allowed_ips"]
    out(f"  abilities:   {', '.join(row['abilities']) or '(none - this key can call nothing)'}")
    out(f"  allowed IPs: {'any' if ips is None else ', '.join(str(ip) for ip in ips) or '(none)'}")
    out(f"  expires:     {_when(row['expires_at'], 'never')}")
    out(f"  acts as:     {'user ' + str(row['user_id']) if row['user_id'] is not None else '-'}")


def cmd_install(migrations_dir: pathlib.Path, table: str, now: datetime.datetime, out: Out) -> int:
    """Write the schema into the project's own migrations directory, like `audit install`."""

    if not migrations_dir.is_dir():
        out(f"error: no migrations directory at {migrations_dir}")
        return 2

    try:
        schema = _TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        out(f"error: could not read {_TEMPLATE}: {e}")
        return 1

    if table != "api_keys":
        schema = schema.replace("api_keys", table)

    try:
        target = migrations_dir / new_filename(f"create {table}", now)
    except MigrationError as e:
        out(f"error: {e}")
        return 2

    if target.exists():
        out(f"error: {target} already exists")
        return 1

    try:
        target.write_text(schema, encoding="utf-8")
    except OSError as e:
        out(f"error: could not write {target}: {e}")
        return 1

    out(f"Wrote {target}")
    out("Review it, then: python3 src/app.py migrations apply")

    return 0


async def cmd_create(
    conn: psycopg.AsyncConnection,
    table: str,
    pepper: str,
    *,
    name: str,
    abilities: list[str],
    allowed_ips: list[str] | None,
    expires_at: datetime.datetime | None,
    user_id: int | None,
    out: Out,
) -> int:
    prefix, secret = generate_key()
    statement = sql.SQL(
        "INSERT INTO {table} (name, key_prefix, secret_hash, abilities, allowed_ips, expires_at, user_id,"
        " created_at, updated_at)"
        " VALUES (%s, %s, %s, %s::text[], %s::cidr[], %s, %s, now(), now())"
        " RETURNING id, abilities, allowed_ips, expires_at, user_id"
    ).format(table=sql.Identifier(table))

    try:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                statement,
                (name, prefix, hash_secret(secret, pepper), abilities, allowed_ips, expires_at, user_id),
            )
            row = await cur.fetchone()
    except psycopg.Error as e:
        out(f"error: cannot create the key: {e}")
        return 1

    assert row is not None
    out(f'Created API key {row["id"]} "{name}" (prefix {prefix})')
    _describe(row, out)
    out("")
    out("The key is shown once and cannot be recovered. Store it now:")
    out(f"{prefix}.{secret}")

    return 0


async def cmd_list(conn: psycopg.AsyncConnection, table: str, out: Out) -> int:
    statement = sql.SQL(
        "SELECT id, name, key_prefix, abilities, allowed_ips, expires_at, user_id, last_used_at, total_uses,"
        " disabled_at FROM {table} ORDER BY id"
    ).format(table=sql.Identifier(table))

    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(statement)
            rows = await cur.fetchall()
    except psycopg.Error as e:
        out(f"error: cannot read {table}: {e}")
        return 1

    if not rows:
        out("No API keys.")
        return 0

    header = ("ID", "NAME", "PREFIX", "STATUS", "ABILITIES", "IPS", "USER", "EXPIRES", "LAST USED", "USES")
    lines = [header]
    for row in rows:
        if row["disabled_at"]:
            status = "revoked"
        elif expired(row["expires_at"]):
            status = "expired"
        else:
            status = "active"

        ips = row["allowed_ips"]
        lines.append(
            (
                str(row["id"]),
                row["name"],
                row["key_prefix"],
                status,
                ",".join(row["abilities"]) or "-",
                "any" if ips is None else ",".join(str(ip) for ip in ips) or "none",
                str(row["user_id"]) if row["user_id"] is not None else "-",
                _when(row["expires_at"], "never"),
                _when(row["last_used_at"], "never"),
                str(row["total_uses"]),
            )
        )

    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    for line in lines:
        out("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())

    return 0


async def cmd_revoke(conn: psycopg.AsyncConnection, table: str, ref: str, out: Out) -> int:
    where, value = _where_ref(ref)
    relation = sql.Identifier(table)

    try:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                sql.SQL("SELECT id, name, disabled_at FROM {table} WHERE {where} FOR UPDATE").format(
                    table=relation, where=where
                ),
                (value,),
            )
            row = await cur.fetchone()
            if row is None:
                out(f"error: no API key {ref!r}")
                return 1

            if row[2] is not None:
                out(f'API key {row[0]} "{row[1]}" was already revoked at {_when(row[2], "")}.')
                return 0

            await cur.execute(
                sql.SQL("UPDATE {table} SET disabled_at = now(), updated_at = now() WHERE id = %s").format(
                    table=relation
                ),
                (row[0],),
            )
    except psycopg.Error as e:
        out(f"error: cannot revoke {ref!r}: {e}")
        return 1

    out(f'Revoked API key {row[0]} "{row[1]}".')
    return 0


async def cmd_update(conn: psycopg.AsyncConnection, table: str, ref: str, change: Update, out: Out) -> int:
    where, value = _where_ref(ref)
    relation = sql.Identifier(table)

    try:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql.SQL("SELECT id, name, abilities FROM {table} WHERE {where} FOR UPDATE").format(
                    table=relation, where=where
                ),
                (value,),
            )
            current = await cur.fetchone()
            if current is None:
                out(f"error: no API key {ref!r}")
                return 1

            assignments: list[sql.Composable] = [sql.SQL("updated_at = now()")]
            params: list[Any] = []

            def assign(column: str, param: Any, cast: LiteralString = "") -> None:
                assignments.append(sql.SQL("{} = %s" + cast).format(sql.Identifier(column)))
                params.append(param)

            if change.name is not None:
                assign("name", change.name)

            if change.abilities is not None or change.add_abilities or change.remove_abilities:
                abilities = list(current["abilities"]) if change.abilities is None else list(change.abilities)
                abilities += [a for a in change.add_abilities or () if a not in abilities]
                abilities = [a for a in abilities if a not in (change.remove_abilities or ())]
                assign("abilities", abilities, "::text[]")

            if change.any_ip:
                assign("allowed_ips", None, "::cidr[]")
            elif change.allowed_ips is not None:
                assign("allowed_ips", change.allowed_ips, "::cidr[]")

            if change.no_expiry:
                assign("expires_at", None)
            elif change.expires_at is not None:
                assign("expires_at", change.expires_at)

            if change.no_user:
                assign("user_id", None)
            elif change.user_id is not None:
                assign("user_id", change.user_id)

            await cur.execute(
                sql.SQL(
                    "UPDATE {table} SET {assignments} WHERE id = %s"
                    " RETURNING id, name, abilities, allowed_ips, expires_at, user_id"
                ).format(table=relation, assignments=sql.SQL(", ").join(assignments)),
                (*params, current["id"]),
            )
            row = await cur.fetchone()
    except psycopg.Error as e:
        out(f"error: cannot update {ref!r}: {e}")
        return 1

    assert row is not None
    out(f'Updated API key {row["id"]} "{row["name"]}"')
    _describe(row, out)

    return 0
