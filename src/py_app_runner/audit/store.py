"""The single writer.

One INSERT per event, on the cursor the caller handed in, inside whatever transaction the
caller already opened. No buffering, no batching, no flush on shutdown - that is not an
omission, it is the whole guarantee: a rolled-back change takes its audit row with it, and
anything that defers the write gives that up.
"""

import datetime
import json
import re
from typing import Any

import psycopg
from psycopg import sql

from py_app_runner.audit.errors import AuditError
from py_app_runner.audit.event import AuditEvent

# The audit table is an identifier, so it cannot be bound as a parameter. Identifier()
# quotes it, but a resolver is application code that may have been handed a value derived
# from a request, so the shape is checked before it gets there.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# Values are cut to fit rather than rejected. An audit write that throws because somebody's
# name is long has turned the trail into an outage.
_WIDTHS = {
    "request_id": 32,
    "module": 64,
    "event": 32,
    "entity_type": 128,
    "entity_id": 64,
    "actor_type": 32,
    "actor_id": 64,
    "actor_name": 190,
    "ip_address": 45,
}

_COLUMNS = (
    "created_at",
    "request_id",
    "module",
    "event",
    "entity_type",
    "entity_id",
    "actor_type",
    "actor_id",
    "actor_name",
    "old_values",
    "new_values",
    "url",
    "ip_address",
    "user_agent",
    "tags",
    "context",
)


def assert_table_name(table: str) -> str:
    if _TABLE_RE.match(table) is None:
        raise AuditError(f"Refusing to write the audit trail to {table!r}: not a plain table name")

    return table


def qualified(table: str) -> sql.Composed:
    """Quote a schema qualifier as two identifiers.

    Identifier("public.audit_log") quotes the dot into the name, producing a single
    relation that does not exist.
    """

    return sql.SQL(".").join(sql.Identifier(part) for part in table.split("."))


def _fit(column: str, value: str) -> str:
    width = _WIDTHS.get(column)
    if width is None or len(value) <= width:
        return value

    return value[:width]


def _json(value: Any) -> str | None:
    if value is None:
        return None

    try:
        # ensure_ascii off so a name keeps its diacritics rather than becoming escapes
        # nobody can read in a SELECT; default=str so a Decimal or a datetime in a payload
        # is recorded rather than failing the whole write.
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # The change itself already happened. Losing the detail of it is bad; failing the
        # request over an unserialisable value in a context dict is worse.
        return None


class Store:
    def __init__(self, table: str = "audit_log") -> None:
        self.table = assert_table_name(table)

    async def write(self, cur: psycopg.AsyncCursor, event: AuditEvent) -> None:
        statement = sql.SQL("INSERT INTO {rel} ({cols}) VALUES ({vals})").format(
            rel=qualified(self.table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in _COLUMNS),
        )

        await cur.execute(statement, self.row(event))

    def row(self, event: AuditEvent) -> tuple[Any, ...]:
        created_at = event.created_at or datetime.datetime.now(datetime.UTC)

        return (
            created_at,
            _fit("request_id", event.request_id),
            _fit("module", event.module),
            _fit("event", event.event),
            _fit("entity_type", event.entity_type),
            _fit("entity_id", event.entity_id),
            _fit("actor_type", event.actor_type),
            _fit("actor_id", event.actor_id),
            _fit("actor_name", event.actor_name),
            _json(event.old_values),
            _json(event.new_values),
            event.url,
            _fit("ip_address", event.ip_address),
            event.user_agent,
            # An empty tag list stores NULL rather than "[]", so "has tags" is a NULL check
            # rather than a json length.
            _json(event.tags) if event.tags else None,
            _json(event.context),
        )
