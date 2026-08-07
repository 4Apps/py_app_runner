import datetime
from decimal import Decimal

from py_app_runner.audit.diff import REDACTED, between, mask, same


class TestSame:
    def test_a_decimal_equals_its_numeric_string(self):
        """psycopg returns numerics as Decimal while the caller hands in an int or a float.
        A str() comparison would call these different and report a change on a column
        nobody touched - which is how a trail becomes something nobody opens."""

        assert same(Decimal("10.50"), 10.5) is True
        assert same(Decimal("10.50"), "10.50") is True
        assert same(Decimal("10.50"), 10.6) is False

    def test_an_int_equals_its_string(self):
        assert same(1, "1") is True
        assert same(1, "2") is False

    def test_a_datetime_equals_its_iso_string(self):
        moment = datetime.datetime(2026, 8, 2, 12, 30, tzinfo=datetime.UTC)
        assert same(moment, "2026-08-02T12:30:00+00:00") is True
        assert same(moment, "2026-08-02T12:31:00+00:00") is False

    def test_a_date_equals_its_iso_string(self):
        assert same(datetime.date(2026, 8, 2), "2026-08-02") is True

    def test_naive_and_aware_datetimes_are_not_silently_reconciled(self):
        """Guessing a timezone for the naive side would shift it, and the shift would only
        be visible in the trail as a change that did not happen."""

        aware = datetime.datetime(2026, 8, 2, 12, 0, tzinfo=datetime.UTC)
        naive = datetime.datetime(2026, 8, 2, 12, 0)

        assert same(aware, naive) is False

    def test_none_only_equals_none(self):
        assert same(None, None) is True
        assert same(None, "") is False
        assert same("", None) is False

    def test_booleans_use_the_truthy_table(self):
        assert same(True, "t") is True
        assert same(True, "true") is True
        assert same(True, 1) is True
        assert same(False, "f") is True
        assert same(False, "") is True
        assert same(True, "no") is False

    def test_the_boolean_table_applies_only_when_one_side_is_a_real_bool(self):
        """Otherwise a text column holding "true" would silently equal one holding "1", and
        a real change would stop being recorded."""

        assert same("true", "1") is False
        assert same(True, "1") is True

    def test_structures_compare_by_content_not_identity(self):
        assert same({"a": 1, "b": 2}, {"b": 2, "a": 1}) is True
        assert same({"a": 1}, {"a": 2}) is False
        assert same([1, 2], [1, 2]) is True


class TestBetween:
    def test_an_insert_records_only_new_values(self):
        old, new = between(None, {"name": "Anna"})
        assert old is None
        assert new == {"name": "Anna"}

    def test_a_delete_records_only_old_values(self):
        old, new = between({"name": "Anna"}, None)
        assert old == {"name": "Anna"}
        assert new is None

    def test_an_update_records_only_what_changed(self):
        """An update is routinely handed three columns against a thirty-column row.
        Reporting the untouched ones as changes to nothing is the failure this prevents."""

        before = {"id": 1, "name": "Anna", "status": "active", "note": "x"}
        after = {"status": "left"}

        old, new = between(before, after)
        assert old == {"status": "active"}
        assert new == {"status": "left"}

    def test_a_column_absent_before_is_a_change_from_none(self):
        """The case a naive dict diff drops silently, and exactly the shape of "this column
        was just populated"."""

        old, new = between({"name": "Anna"}, {"approved_at": "2026-08-02"})
        assert old == {"approved_at": None}
        assert new == {"approved_at": "2026-08-02"}

    def test_no_change_collapses_to_a_pair_of_nones(self):
        """Lets the caller read "nothing happened" without inspecting the contents, and is
        what stops an update recording an event per row it did not alter."""

        assert between({"name": "Anna"}, {"name": "Anna"}) == (None, None)

    def test_nothing_at_all_is_also_a_pair_of_nones(self):
        assert between(None, None) == (None, None)


class TestRedaction:
    def test_an_excluded_column_keeps_its_key_and_loses_its_value(self):
        """The trail should still show that a password changed."""

        old, new = between({"password": "old-hash"}, {"password": "new-hash"}, ["password"])
        assert old == {"password": REDACTED}
        assert new == {"password": REDACTED}

    def test_an_excluded_column_that_did_not_change_is_not_recorded_at_all(self):
        """Comparison runs before redaction, so an unchanged secret produces no row rather
        than a row full of asterisks."""

        assert between({"password": "same"}, {"password": "same"}, ["password"]) == (None, None)

    def test_redaction_applies_to_a_whole_row_on_insert_and_delete(self):
        _, new = between(None, {"name": "Anna", "password": "secret"}, ["password"])
        assert new == {"name": "Anna", "password": REDACTED}

        old, _ = between({"name": "Anna", "password": "secret"}, None, ["password"])
        assert old == {"name": "Anna", "password": REDACTED}

    def test_mask_leaves_a_row_alone_when_nothing_is_excluded(self):
        row = {"a": 1}
        assert mask(row, []) is row
