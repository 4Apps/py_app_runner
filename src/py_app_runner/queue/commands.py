"""Command implementations, importable directly for programmatic use."""

import datetime
import pathlib
import re
from collections.abc import Callable
from typing import Any

import psycopg

from py_app_runner.migrations.discovery import MigrationError, new_filename
from py_app_runner.queue.interface import QueueDriver
from py_app_runner.queue.job import QueueError

Out = Callable[[str], None]

_TEMPLATE = pathlib.Path(__file__).parent / "files" / "install.pgsql.sql"

_BEFORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")


def cmd_install(
    migrations_dir: pathlib.Path,
    table: str,
    failed_table: str,
    now: datetime.datetime,
    out: Out,
) -> int:
    """Write the queue schema into the project's own migrations directory."""

    if not migrations_dir.is_dir():
        out(f"error: no migrations directory at {migrations_dir}")
        return 2

    try:
        schema = _TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        out(f"error: could not read {_TEMPLATE}: {e}")
        return 1

    # The failed table is substituted first. "queue_jobs" is a prefix of nothing, but
    # "queue_failed_jobs" contains "queue_jobs" nowhere while its *index* names derive from
    # it - do the longer name first and neither can be half-rewritten by the other.
    if failed_table != "queue_failed_jobs":
        schema = schema.replace("queue_failed_jobs", failed_table)
    if table != "queue_jobs":
        schema = schema.replace("queue_jobs", table)

    try:
        target = migrations_dir / new_filename("create queue tables", now)
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


async def cmd_status(queue: QueueDriver, out: Out) -> int:
    try:
        rows = await queue.stats()
        failed = await queue.failed_count()
    except (psycopg.Error, QueueError) as e:
        out(f"error: cannot read the queue: {e}")
        return 1

    if not rows:
        out("No jobs queued.")
    else:
        out(f"{'queue':<24} {'pending':>9} {'delayed':>9} {'held':>9} {'total':>9}")
        for row in rows:
            out(f"{row['queue']:<24} {row['pending']:>9} {row['delayed']:>9} {row['reserved']:>9} {row['total']:>9}")

    out("")
    out(f"failed: {failed}")
    if failed > 0:
        out("Read them with: python3 src/app.py queue failed")

    return 0


async def cmd_failed(queue: QueueDriver, limit: int, out: Out) -> int:
    if limit < 1:
        out("error: --limit must be at least 1")
        return 2

    try:
        rows = await queue.failed_rows(limit)
    except (psycopg.Error, QueueError) as e:
        out(f"error: cannot read the queue: {e}")
        return 1

    if not rows:
        out("Nothing has failed.")
        return 0

    for row in rows:
        out(
            f"#{str(row['id']):<6} {str(row['failed_at']):<26} {row['queue']:<16} "
            f"{row['name']} ({row['attempts']} attempts)"
        )
        # Only the first line: a traceback in a list view buries the next entry.
        first_line = (row["error"] or "").split("\n", 1)[0]
        if first_line:
            out(f"    {first_line}")

    out("")
    out("Requeue one with: python3 src/app.py queue retry --id N")

    return 0


async def cmd_retry(queue: QueueDriver, job_id: int | None, all_jobs: bool, max_attempts: int, out: Out) -> int:
    if job_id is None and not all_jobs:
        out("error: retry needs --id N or --all")
        return 2

    try:
        count = await queue.retry_failed(job_id, max_attempts)
    except (psycopg.Error, QueueError) as e:
        out(f"error: could not requeue: {e}")
        return 1

    if count == 0:
        out("Nothing to requeue.")
        # Naming a specific id that is not there is a failed instruction; --all finding
        # nothing is a legitimately empty table.
        return 0 if job_id is None else 1

    out(f"Requeued {count} job(s).")
    return 0


async def cmd_forget(
    queue: QueueDriver,
    job_id: int | None,
    all_jobs: bool,
    before: str | None,
    out: Out,
) -> int:
    if job_id is None and not all_jobs and before is None:
        out("error: forget needs --id N, --all or --before DATE")
        return 2

    if before is not None and _BEFORE_RE.match(before) is None:
        out(f"error: --before={before!r} must be YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'")
        return 2

    try:
        count = await queue.forget_failed(job_id, before)
    except (psycopg.Error, QueueError) as e:
        out(f"error: could not forget: {e}")
        return 1

    out(f"Deleted {count} row(s).")
    return 0


def parse_queues(raw: str | None, default: str = "default") -> list[str]:
    """Comma separated, in precedence order. Blanks dropped so a trailing comma is not a
    queue named "" that never has work."""

    names = [part.strip() for part in (raw or default).split(",")]
    return [name for name in names if name] or [default]


def resolve_handlers(config: dict[str, Any]) -> dict[str, Any]:
    return (config.get("queue") or {}).get("handlers") or {}
