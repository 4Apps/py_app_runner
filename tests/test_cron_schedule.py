import datetime
import zoneinfo

import pytest

from py_app_runner.cron.schedule import CronError, current_minute, load_jobs

RIGA = zoneinfo.ZoneInfo("Europe/Riga")


def config(jobs: dict, **cron) -> dict:
    return {"cron": {"jobs": jobs, **cron}}


class TestLoadJobs:
    def test_absent_config_means_no_jobs(self):
        assert load_jobs({}) == []

    def test_parses_command_and_applies_default_timezone(self):
        [job] = load_jobs(config({"sync": {"schedule": "0 4 * * 0", "command": 'parcel lad sync --name "a b"'}}))

        assert job.name == "sync"
        assert job.command == ("parcel", "lad", "sync", "--name", "a b")
        assert str(job.timezone) == "UTC"
        assert job.timeout is None

    def test_job_timezone_overrides_default(self):
        [job] = load_jobs(
            config({"a": {"schedule": "0 4 * * *", "command": "x", "timezone": "Europe/Riga"}}, timezone="UTC")
        )

        assert job.timezone == RIGA

    @pytest.mark.parametrize(
        "raw, fragment",
        [
            ({"command": "x"}, "schedule must be"),
            ({"schedule": "0 4 * * *"}, "command must be"),
            ({"schedule": "0 4 * *", "command": "x"}, "bad schedule"),
            ({"schedule": "0 4 * * *", "command": "x", "timeout": 0}, "timeout must be"),
            ({"schedule": "0 4 * * *", "command": "x", "timeout": True}, "timeout must be"),
            ({"schedule": "0 4 * * *", "command": "x", "timezone": "Mars/Olympus"}, "unknown timezone"),
            ({"schedule": "0 4 * * *", "command": "x", "scheduel": "1"}, "unknown key"),
            ("0 4 * * * x", "expected a dict"),
        ],
    )
    def test_malformed_job_is_refused_by_name(self, raw, fragment):
        with pytest.raises(CronError, match=f"Job 'bad'.*{fragment}"):
            load_jobs(config({"bad": raw}))

    def test_bad_default_timezone_is_refused(self):
        with pytest.raises(CronError, match="unknown timezone"):
            load_jobs(config({}, timezone="Nowhere/Land"))


class TestDue:
    def test_due_only_on_the_matching_minute_in_its_own_zone(self):
        [job] = load_jobs(config({"a": {"schedule": "30 4 * * *", "command": "x", "timezone": "Europe/Riga"}}))

        # 04:30 Riga in September is 01:30 UTC.
        assert job.is_due(datetime.datetime(2026, 9, 7, 1, 30, tzinfo=datetime.UTC))
        assert job.is_due(datetime.datetime(2026, 9, 7, 1, 30, 45, tzinfo=datetime.UTC))
        assert not job.is_due(datetime.datetime(2026, 9, 7, 1, 31, tzinfo=datetime.UTC))
        assert not job.is_due(datetime.datetime(2026, 9, 7, 4, 30, tzinfo=datetime.UTC))

    def test_next_run_is_reported_in_the_job_zone(self):
        [job] = load_jobs(config({"a": {"schedule": "0 4 * * 0", "command": "x", "timezone": "Europe/Riga"}}))

        nxt = job.next_run(datetime.datetime(2026, 9, 7, 10, 0, tzinfo=datetime.UTC))

        assert nxt == datetime.datetime(2026, 9, 13, 4, 0, tzinfo=RIGA)

    def test_current_minute_drops_seconds(self):
        now = datetime.datetime(2026, 9, 7, 10, 5, 59, 123, tzinfo=datetime.UTC)

        assert current_minute(now) == datetime.datetime(2026, 9, 7, 10, 5, tzinfo=datetime.UTC)
