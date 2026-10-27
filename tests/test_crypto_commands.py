import psycopg
import pytest_asyncio

from py_app_runner.crypto.commands import cmd_rotate
from py_app_runner.crypto.fields import FieldCrypto, generate_key, key_id_of
from tests.migrations_pg import pg_dsn_for

ENV = {"APP_K1": generate_key(), "APP_K2": generate_key()}
KEYS = {"k1": "APP_K1", "k2": "APP_K2"}


@pytest_asyncio.fixture
async def db_dsn():
    async with pg_dsn_for("par_test_crypto_rotate") as dsn:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            await connection.execute("CREATE TABLE people (id serial PRIMARY KEY, secret text)")
        yield dsn


@pytest_asyncio.fixture
async def conn(db_dsn):
    async with await psycopg.AsyncConnection.connect(db_dsn, autocommit=True) as connection:
        yield connection


class WritesDuringDecrypt(FieldCrypto):
    """Stands in for the application writing the row between rotate's read and its write."""

    def __init__(self, dsn: str, replacement: str) -> None:
        super().__init__("k2", KEYS, resolver=ENV.get)
        self.dsn = dsn
        self.replacement = replacement

    def decrypt(self, value: str | None) -> str | None:
        with psycopg.connect(self.dsn, autocommit=True) as other:
            other.execute("UPDATE people SET secret = %s", (self.replacement,))
        return super().decrypt(value)


class TestRotate:
    async def test_rotates_every_row_across_pages(self, conn):
        k1 = FieldCrypto("k1", KEYS, resolver=ENV.get)
        for plain in ("a", "b", "c"):
            await conn.execute("INSERT INTO people (secret) VALUES (%s)", (k1.encrypt(plain),))
        k2 = FieldCrypto("k2", KEYS, resolver=ENV.get)

        assert await cmd_rotate(conn, k2, "people", "secret", "id", 2, False, lambda _line: None) == 0

        rows = await (await conn.execute("SELECT secret FROM people ORDER BY id")).fetchall()
        assert [(key_id_of(row[0]), k2.decrypt(row[0])) for row in rows] == [("k2", "a"), ("k2", "b"), ("k2", "c")]

    async def test_a_value_written_while_rotating_is_kept(self, conn, db_dsn):
        old = FieldCrypto("k1", KEYS, resolver=ENV.get).encrypt("old")
        await conn.execute("INSERT INTO people (secret) VALUES (%s)", (old,))
        crypto = WritesDuringDecrypt(db_dsn, FieldCrypto("k2", KEYS, resolver=ENV.get).encrypt("new"))
        lines: list[str] = []

        assert await cmd_rotate(conn, crypto, "people", "secret", "id", 100, False, lines.append) == 0

        stored = (await (await conn.execute("SELECT secret FROM people")).fetchone())[0]
        assert crypto.decrypt(stored) == "new"
        assert "0 re-encrypted." in lines
        assert any("1 were written by someone else" in line for line in lines)
