import datetime as dt

from py_app_runner.utils import create_valid_hostname, json_encoder


class TestJsonEncoderDatetimes:
    def test_aware_datetime_is_normalized_to_utc(self):
        # The wire format carries no offset, so an aware value must be converted
        # rather than formatted as a local wall clock time.
        riga = dt.timezone(dt.timedelta(hours=3))
        value = dt.datetime(2026, 7, 27, 12, 0, 0, tzinfo=riga)

        assert json_encoder(value) == "2026-07-27T09:00:00"

    def test_naive_datetime_is_left_alone(self):
        value = dt.datetime(2026, 7, 27, 12, 0, 0)
        assert json_encoder(value) == "2026-07-27T12:00:00"

    def test_date_is_unaffected(self):
        assert json_encoder(dt.date(2026, 7, 27)) == "2026-07-27"


class TestCreateValidHostname:
    def test_empty_input_returns_empty(self):
        assert create_valid_hostname("") == ""

    def test_strips_surrounding_hyphens(self):
        assert create_valid_hostname(" my host ") == "my-host"

    def test_only_separators_collapse_to_empty(self):
        assert create_valid_hostname("...") == ""
