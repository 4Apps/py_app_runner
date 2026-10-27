import datetime as dt

import pytest

from py_app_runner.utils import create_valid_hostname, format_body_for_log, is_sensitive_key, json_encoder


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


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("POSTGRES_PASSWORD", True),
        ("pg_pass", True),
        ("azure_client_secret", True),
        ("X-Auth-Token", True),
        ("sentry_dsn", True),
        ("ssh_private_key", True),
        ("user_name", True),
        ("id_token_jwt", True),
        ("aws_access_key_id", True),
        ("otp_code", True),
        ("totp", True),
        ("footprint", False),
        ("mapping", False),
        ("refresh_interval", False),
        ("author", False),
        ("authority", False),
        ("user_id", False),
        ("user_agent", False),
        ("sass_file", False),
        ("hostname", False),
        ("port", False),
    ],
)
def test_is_sensitive_key(key: str, sensitive: bool):
    assert is_sensitive_key(key) is sensitive


def test_form_encoded_body_is_redacted_by_key():
    body = b"username=alice&pass%77ord=hunter2&remember=1&csrf_token=abc123"

    logged = format_body_for_log(body, max_chars=4000, shrink_strings_to=256, cap_list_items=50)

    for secret in ("alice", "hunter2", "abc123"):
        assert secret not in logged, secret
    assert "remember=1" in logged
