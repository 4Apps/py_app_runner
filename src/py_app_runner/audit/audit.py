"""The call-site API.

Every audit row is the result of an explicit call. There is no ORM hook, no middleware and
no database trigger, because a trail that writes itself is one nobody can reason about at
the call site - and because a hook that does not fire produces a silent gap rather than a
loud error.

    await audit.insert(cur, "people", {"name": "Anna"}, module="hr")
    await audit.update(cur, "people", {"status": "left"}, {"id": 42}, module="hr")

`update` and `delete` read the affected rows before writing, so old values are recorded
without hand-written before-fetches at every call site.
"""

import logging
from typing import Any

import psycopg
from psycopg import sql

from py_app_runner.audit import diff
from py_app_runner.audit.errors import AuditError
from py_app_runner.audit.event import CREATED, DELETED, UPDATED, AuditEvent, current_context
from py_app_runner.audit.store import Store, assert_table_name, qualified

_logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "table": "audit_log",
    "strict": True,
    "max_rows": 1000,
    "id_key": "id",
    "exclude": {},
}


class Audit:
    def __init__(
        self,
        table: str = "audit_log",
        strict: bool = True,
        max_rows: int = 1000,
        id_key: str = "id",
        exclude: dict[str, list[str]] | None = None,
    ) -> None:
        self.store = Store(table)
        self.strict = strict
        self.max_rows = max_rows
        self.id_key = id_key
        self.exclude = self._validated_exclude(exclude or {})

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "Audit":
        settings = {**_DEFAULTS, **(config.get("audit") or {})}

        max_rows = settings["max_rows"]
        if not isinstance(max_rows, int) or isinstance(max_rows, bool):
            raise AuditError(
                f'config["audit"]["max_rows"] must be an int; got {max_rows!r}. '
                f"A string here would compare against a row count and silently never match."
            )

        return cls(
            table=settings["table"],
            strict=settings["strict"] is not False,
            max_rows=max_rows,
            id_key=settings["id_key"] or "id",
            exclude=settings["exclude"] or {},
        )

    def _validated_exclude(self, exclude: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject a malformed exclude block at construction.

        Redaction fails open by its nature: a lookup that finds nothing to exclude simply
        excludes nothing, so a mistake here leaks exactly the values it was meant to
        withhold, silently and forever. Table names cannot be checked against the database
        from here, but the shape can be, and that catches the common mistake of writing a
        bare list where a mapping belongs.
        """

        if not isinstance(exclude, dict):
            raise AuditError(
                f'config["audit"]["exclude"] must be a mapping of table name -> list of '
                f"columns; got {type(exclude).__name__}."
            )

        for table, columns in exclude.items():
            if not isinstance(columns, (list, tuple)) or not all(isinstance(c, str) for c in columns):
                raise AuditError(
                    f'config["audit"]["exclude"][{table!r}] must be a list of column names; '
                    f"got {columns!r}. Anything unreadable here is a redaction that will not happen."
                )

        return {table: list(columns) for table, columns in exclude.items()}

    def excluded(self, table: str) -> list[str]:
        return self.exclude.get(table, [])

    ################
    ### Recording ##
    ################

    async def record(self, cur: psycopg.AsyncCursor, event: AuditEvent) -> None:
        """Write one event, on the caller's cursor and inside the caller's transaction."""

        try:
            await self.store.write(cur, event.with_resolved(current_context()))
        except Exception as e:
            self._fail(e)

    def _fail(self, error: Exception) -> None:
        if self.strict:
            if isinstance(error, AuditError):
                raise error

            raise AuditError(f"Audit trail: {error}") from error

        # Availability over completeness, which is rarely the trade an audit trail wants to
        # make. It is a choice, not a silence: the line is always logged.
        #
        # Note what this cannot do on Postgres: a failed INSERT aborts the surrounding
        # transaction, so swallowing the exception does not rescue the change - it converts
        # a clear failure into an unattributable one at commit. Callers running inside a
        # transaction should use a SAVEPOINT around the change if they set strict=False.
        _logger.warning("Audit trail: %s", error)

    ################
    ### Wrappers ###
    ################

    async def insert(
        self,
        cur: psycopg.AsyncCursor,
        table: str,
        data: dict[str, Any],
        module: str = "",
        entity_id: str | None = None,
        tags: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Insert a row and record it. Returns the inserted key.

        Always uses RETURNING rather than reading the id back afterwards. Postgres cannot
        answer "what id did I just insert" without being told which sequence to look at, so
        every fallback for it either guesses or returns nothing - and returning nothing here
        quietly empties the column `idx_audit_log_entity` exists to search.
        """

        assert_table_name(table)
        columns = list(data.keys())

        statement = sql.SQL("INSERT INTO {rel} ({cols}) VALUES ({vals}) RETURNING {id}").format(
            rel=qualified(table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            id=sql.Identifier(self.id_key),
        )

        await cur.execute(statement, tuple(data[c] for c in columns))
        returned = await cur.fetchone()
        new_id = returned[0] if returned else None

        _, new_values = diff.between(None, data, self.excluded(table))

        await self.record(
            cur,
            AuditEvent(
                event=CREATED,
                entity_type=table,
                entity_id=str(entity_id if entity_id is not None else (new_id if new_id is not None else "")),
                module=module,
                new_values=new_values,
                tags=tags or [],
                context=context,
            ),
        )

        return new_id

    async def update(
        self,
        cur: psycopg.AsyncCursor,
        table: str,
        data: dict[str, Any],
        where: dict[str, Any],
        module: str = "",
        tags: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        """Update rows and record one event per row that actually changed."""

        assert_table_name(table)
        rows = await self._rows(cur, table, where)
        self._assert_within_limit(table, len(rows))

        columns = list(data.keys())
        statement = sql.SQL("UPDATE {rel} SET {sets} WHERE {cond}").format(
            rel=qualified(table),
            sets=sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c in columns),
            cond=self._condition(where),
        )

        await cur.execute(statement, tuple(data[c] for c in columns) + tuple(where.values()))
        affected = cur.rowcount

        if not rows:
            # Not an error, but it is what a mistyped condition looks like.
            _logger.debug("Audit trail: update on %r matched no rows", table)

        excluded = self.excluded(table)
        for row in rows:
            old_values, new_values = diff.between(row, data, excluded)
            if new_values is None:
                continue

            await self.record(
                cur,
                AuditEvent(
                    event=UPDATED,
                    entity_type=table,
                    entity_id=self._row_id(row),
                    module=module,
                    old_values=old_values,
                    new_values=new_values,
                    tags=tags or [],
                    context=context,
                ),
            )

        return affected

    async def delete(
        self,
        cur: psycopg.AsyncCursor,
        table: str,
        where: dict[str, Any],
        module: str = "",
        tags: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        """Delete rows and record each one, carrying the whole row as old values."""

        assert_table_name(table)

        if not where:
            # An empty condition builds no WHERE at all, so it deletes the table and audits
            # every row of it. Nothing below this call would refuse that, so it is refused
            # here.
            raise AuditError(
                f"Refusing to delete every row of {table!r} with an empty condition. "
                f"Pass an explicit condition, or run the DELETE yourself and record it with record()."
            )

        rows = await self._rows(cur, table, where)
        self._assert_within_limit(table, len(rows))

        statement = sql.SQL("DELETE FROM {rel} WHERE {cond}").format(rel=qualified(table), cond=self._condition(where))
        await cur.execute(statement, tuple(where.values()))
        affected = cur.rowcount

        excluded = self.excluded(table)
        for row in rows:
            old_values, _ = diff.between(row, None, excluded)

            await self.record(
                cur,
                AuditEvent(
                    event=DELETED,
                    entity_type=table,
                    entity_id=self._row_id(row),
                    module=module,
                    old_values=old_values,
                    tags=tags or [],
                    context=context,
                ),
            )

        return affected

    ###############
    ### Helpers ###
    ###############

    def _condition(self, where: dict[str, Any]) -> sql.Composed:
        return sql.SQL(" AND ").join(sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c in where)

    async def _rows(self, cur: psycopg.AsyncCursor, table: str, where: dict[str, Any]) -> list[dict[str, Any]]:
        """Read the rows a change is about to affect, as dicts."""

        statement = sql.SQL("SELECT * FROM {rel}{cond}").format(
            rel=qualified(table),
            cond=sql.SQL(" WHERE ") + self._condition(where) if where else sql.SQL(""),
        )

        await cur.execute(statement, tuple(where.values()))
        if cur.description is None:
            return []

        names = [d.name for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in await cur.fetchall()]

    def _row_id(self, row: dict[str, Any]) -> str:
        value = row.get(self.id_key)
        return "" if value is None else str(value)

    def _assert_within_limit(self, table: str, matched: int) -> None:
        """Checked before the write, so a mistyped condition matching the whole table is
        refused rather than becoming one write plus half a million audit rows."""

        if self.max_rows < 1 or matched <= self.max_rows:
            return

        self._fail(
            AuditError(
                f"Refusing to audit {matched} rows of {table!r} in one call; "
                f'config["audit"]["max_rows"] is {self.max_rows}. Narrow the condition, or raise '
                f"the limit."
            )
        )
