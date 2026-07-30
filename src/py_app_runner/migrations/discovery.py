import hashlib
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime

MIGRATION_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-([a-z0-9-]+)\.sql$")
NO_TRANSACTION_DIRECTIVE = "-- migrations:no-transaction"

# psql meta-commands. pg_dump emits \restrict / \unrestrict, and psycopg sends SQL to the
# server directly - there is no psql to interpret these, so they arrive as syntax errors.
_META_COMMAND_RE = re.compile(r"^\s*\\")


class MigrationError(Exception):
    """Anything wrong with the migration files themselves, as opposed to the SQL failing."""


@dataclass(frozen=True)
class MigrationFile:
    name: str
    prefix: str
    path: pathlib.Path
    sql: str
    checksum: str
    no_transaction: bool


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_migration(path: pathlib.Path) -> MigrationFile:
    match = MIGRATION_FILENAME_RE.match(path.name)
    if not match:
        raise MigrationError(f"Bad migration filename {path.name!r}: expected YYYY-MM-DD-HHMMSS-kebab-name.sql")

    raw = path.read_bytes()
    try:
        sql = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"Migration {path.name!r} is not valid UTF-8: {exc}") from exc

    first_line = sql.split("\n", 1)[0].strip()

    return MigrationFile(
        name=path.name,
        prefix=match.group(1),
        path=path,
        sql=sql,
        checksum=checksum_bytes(raw),
        no_transaction=first_line == NO_TRANSACTION_DIRECTIVE,
    )


def discover(directory: pathlib.Path) -> list[MigrationFile]:
    """Every *.sql in `directory`, chronological. Filenames sort chronologically as plain
    text, which is the whole point of the timestamp prefix."""

    if not directory.is_dir():
        raise MigrationError(f"Migrations directory does not exist: {directory}")

    migrations = [load_migration(path) for path in sorted(directory.glob("*.sql"))]

    seen: dict[str, str] = {}
    for migration in migrations:
        if migration.prefix in seen:
            raise MigrationError(
                f"Duplicate migration timestamp {migration.prefix}: {seen[migration.prefix]} and {migration.name}"
            )

        seen[migration.prefix] = migration.name

    return migrations


def find_meta_commands(sql: str) -> list[tuple[int, str]]:
    """1-indexed line numbers of psql meta-commands. A backslash inside a string literal is
    not one, and only a line that *starts* with a backslash can be."""

    return [
        (number, line.rstrip())
        for number, line in enumerate(sql.split("\n"), start=1)
        if _META_COMMAND_RE.match(line)
    ]


def new_filename(name: str, now: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise MigrationError(f"Migration name {name!r} contains no usable characters")

    return f"{now:%Y-%m-%d-%H%M%S}-{slug}.sql"
