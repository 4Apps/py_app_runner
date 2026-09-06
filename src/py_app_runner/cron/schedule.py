"""What is scheduled, and when it is due.

A job is an `app.py` subcommand and a five-field cron expression, declared as data under
`config["cron"]["jobs"]`. There is no handler import and no run history: a job is due when
its expression matches the current minute, exactly as the system cron that calls `cron run`
would see it, so a tick that was missed is simply missed.
"""

import datetime
import shlex
import zoneinfo
from dataclasses import dataclass
from typing import Any

try:
    from cronsim import CronSim, CronSimError
except ImportError as e:
    raise ImportError("py_app_runner.cron requires the 'cron' extra: pip install 'py_app_runner[cron]'") from e

_DEFAULTS: dict[str, Any] = {
    "db": "main",
    "timezone": "UTC",
    "jobs": {},
}

_JOB_KEYS = frozenset({"schedule", "command", "timeout", "timezone"})


class CronError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    schedule: str
    command: tuple[str, ...]
    timezone: datetime.tzinfo
    timeout: int | None

    def is_due(self, minute: datetime.datetime) -> bool:
        """True when the expression matches this exact minute in the job's own zone."""

        local = minute.astimezone(self.timezone).replace(second=0, microsecond=0)
        # CronSim yields strictly after its start, so stepping back a minute makes the
        # given minute itself the first candidate.
        return next(CronSim(self.schedule, local - datetime.timedelta(minutes=1))) == local

    def next_run(self, after: datetime.datetime) -> datetime.datetime:
        return next(CronSim(self.schedule, after.astimezone(self.timezone)))


def settings(config: dict[str, Any]) -> dict[str, Any]:
    return {**_DEFAULTS, **(config.get("cron") or {})}


def _zone(name: Any, job: str) -> datetime.tzinfo:
    if not isinstance(name, str) or not name:
        raise CronError(f"Job {job!r}: timezone must be a non-empty string.")

    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
        raise CronError(f"Job {job!r}: unknown timezone {name!r}.") from e


def _job(name: str, raw: Any, default_zone: str) -> Job:
    if not isinstance(raw, dict):
        raise CronError(f"Job {name!r}: expected a dict with schedule and command.")

    unknown = set(raw) - _JOB_KEYS
    if unknown:
        raise CronError(f"Job {name!r}: unknown key(s) {', '.join(sorted(unknown))}.")

    schedule = raw.get("schedule")
    if not isinstance(schedule, str) or not schedule.strip():
        raise CronError(f"Job {name!r}: schedule must be a cron expression string.")

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise CronError(f"Job {name!r}: command must be a non-empty string of app.py arguments.")

    timeout = raw.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1):
        raise CronError(f"Job {name!r}: timeout must be a positive number of seconds.")

    zone = _zone(raw.get("timezone", default_zone), name)

    try:
        CronSim(schedule, datetime.datetime.now(zone))
    except CronSimError as e:
        raise CronError(f"Job {name!r}: bad schedule {schedule!r}: {e}") from e

    return Job(
        name=name,
        schedule=schedule.strip(),
        command=tuple(shlex.split(command)),
        timezone=zone,
        timeout=timeout,
    )


def load_jobs(config: dict[str, Any]) -> list[Job]:
    """Validate every job up front. A typo in one schedule is a config error for the whole
    service, found on the first `cron list`, rather than a job that silently never fires."""

    conf = settings(config)

    jobs = conf["jobs"]
    if not isinstance(jobs, dict):
        raise CronError('config["cron"]["jobs"] must be a dict of name -> job.')

    default_zone = conf["timezone"]
    _zone(default_zone, "<default>")

    result = []
    for name, raw in jobs.items():
        if not isinstance(name, str) or not name.strip():
            raise CronError("Every job needs a non-empty name.")

        result.append(_job(name, raw, default_zone))

    return result


def current_minute(now: datetime.datetime | None = None) -> datetime.datetime:
    now = now or datetime.datetime.now(datetime.UTC)
    return now.replace(second=0, microsecond=0)
