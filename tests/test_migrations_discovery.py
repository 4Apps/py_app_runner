import datetime
import hashlib

import pytest

from py_app_runner.migrations.discovery import (
    MigrationError,
    checksum_bytes,
    discover,
    find_meta_commands,
    load_migration,
    new_filename,
)


def write(directory, name: str, body: str = "SELECT 1;\n"):
    path = directory / name
    path.write_text(body)
    return path


class TestChecksum:
    def test_is_sha256_of_the_raw_bytes(self):
        assert checksum_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_whitespace_change_changes_the_checksum(self, tmp_path):
        one = load_migration(write(tmp_path, "2026-08-04-091530-a.sql", "SELECT 1;\n"))
        (tmp_path / "2026-08-04-091530-a.sql").write_text("SELECT  1;\n")
        two = load_migration(tmp_path / "2026-08-04-091530-a.sql")
        assert one.checksum != two.checksum


class TestDiscover:
    def test_orders_chronologically_by_filename(self, tmp_path):
        write(tmp_path, "2026-08-04-164512-b.sql")
        write(tmp_path, "2026-07-29-000001-a.sql")
        write(tmp_path, "2026-08-04-091530-c.sql")

        assert [m.name for m in discover(tmp_path)] == [
            "2026-07-29-000001-a.sql",
            "2026-08-04-091530-c.sql",
            "2026-08-04-164512-b.sql",
        ]

    def test_rejects_a_malformed_filename(self, tmp_path):
        write(tmp_path, "026-add-widgets.sql")

        with pytest.raises(MigrationError, match="026-add-widgets.sql"):
            discover(tmp_path)

    def test_rejects_uppercase_and_underscores_in_the_name_part(self, tmp_path):
        write(tmp_path, "2026-08-04-091530-Add_Widgets.sql")

        with pytest.raises(MigrationError, match="Add_Widgets"):
            discover(tmp_path)

    def test_rejects_a_duplicate_timestamp_prefix(self, tmp_path):
        write(tmp_path, "2026-08-04-091530-a.sql")
        write(tmp_path, "2026-08-04-091530-b.sql")

        with pytest.raises(MigrationError, match="2026-08-04-091530"):
            discover(tmp_path)

    def test_ignores_non_sql_files(self, tmp_path):
        write(tmp_path, "2026-08-04-091530-a.sql")
        (tmp_path / "README.md").write_text("notes")
        (tmp_path / ".keep").write_text("")

        assert [m.name for m in discover(tmp_path)] == ["2026-08-04-091530-a.sql"]

    def test_missing_directory_is_an_error(self, tmp_path):
        with pytest.raises(MigrationError, match="does not exist"):
            discover(tmp_path / "nope")

    def test_empty_directory_yields_nothing(self, tmp_path):
        assert discover(tmp_path) == []


class TestNoTransactionDirective:
    def test_detected_on_line_one(self, tmp_path):
        path = write(
            tmp_path,
            "2026-08-04-091530-a.sql",
            "-- migrations:no-transaction\nCREATE INDEX CONCURRENTLY i ON t (c);\n",
        )
        assert load_migration(path).no_transaction is True

    def test_ignored_below_line_one(self, tmp_path):
        path = write(
            tmp_path,
            "2026-08-04-091530-a.sql",
            "-- a comment\n-- migrations:no-transaction\nSELECT 1;\n",
        )
        assert load_migration(path).no_transaction is False

    def test_absent_by_default(self, tmp_path):
        assert load_migration(write(tmp_path, "2026-08-04-091530-a.sql")).no_transaction is False


class TestMetaCommands:
    def test_finds_pg_dump_restrict_markers(self):
        sql = "SET x = 1;\n\\restrict abc123\nCREATE TABLE t ();\n\\unrestrict abc123\n"
        assert find_meta_commands(sql) == [(2, "\\restrict abc123"), (4, "\\unrestrict abc123")]

    def test_ignores_a_backslash_inside_a_string_literal(self):
        assert find_meta_commands("SELECT E'\\\\n' AS nl;\n") == []

    def test_clean_sql_reports_nothing(self):
        assert find_meta_commands("CREATE TABLE t ();\nSELECT 1;\n") == []


class TestNewFilename:
    def test_builds_a_timestamped_kebab_name(self):
        now = datetime.datetime(2026, 8, 4, 9, 15, 30, tzinfo=datetime.UTC)
        assert new_filename("add widgets table", now) == "2026-08-04-091530-add-widgets-table.sql"

    def test_normalises_underscores_and_case(self):
        now = datetime.datetime(2026, 8, 4, 9, 15, 30, tzinfo=datetime.UTC)
        assert new_filename("Add_Widgets_Table", now) == "2026-08-04-091530-add-widgets-table.sql"

    def test_rejects_a_name_that_normalises_to_nothing(self):
        now = datetime.datetime(2026, 8, 4, 9, 15, 30, tzinfo=datetime.UTC)
        with pytest.raises(MigrationError, match="name"):
            new_filename("!!!", now)
