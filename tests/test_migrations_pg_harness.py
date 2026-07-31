"""The integration suite could go all-green having tested nothing.

`pg_dsn_for` used to catch any connection failure and `pytest.skip`, so a CI run started
before Postgres accepted connections skipped every migration test and reported success. The
skip is now opt-in via `ALLOW_PG_SKIP`; by default an unreachable database is a failure.
"""

import psycopg
import pytest

from tests import migrations_pg


class TestPgSkipGate:
    async def test_an_unreachable_database_fails_by_default(self, monkeypatch):
        monkeypatch.delenv("ALLOW_PG_SKIP", raising=False)
        monkeypatch.setattr(migrations_pg, "PG_HOST", "no-such-host-at-all.invalid")

        # Deliberately broad: `pytest.skip` raises `Skipped`, which derives from
        # `BaseException`. Catching only `OperationalError` would let the old behaviour skip
        # this test rather than fail it - the very way the bug hid in the first place.
        with pytest.raises(BaseException) as excinfo:  # noqa: B017
            async with migrations_pg.pg_dsn_for("par_test_never_created"):
                raise AssertionError("the body must never run without a database")

        assert not isinstance(excinfo.value, pytest.skip.Exception), "an unreachable database was skipped over"
        assert isinstance(excinfo.value, psycopg.OperationalError)

    async def test_the_skip_is_available_when_explicitly_opted_into(self, monkeypatch):
        monkeypatch.setenv("ALLOW_PG_SKIP", "1")
        monkeypatch.setattr(migrations_pg, "PG_HOST", "no-such-host-at-all.invalid")

        with pytest.raises(pytest.skip.Exception):
            async with migrations_pg.pg_dsn_for("par_test_never_created"):
                raise AssertionError("the body must never run without a database")
