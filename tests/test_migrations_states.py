import datetime
import pathlib

from py_app_runner.migrations.discovery import MigrationFile
from py_app_runner.migrations.states import (
    AppliedRow,
    State,
    blocking,
    compute_states,
    pending,
)

NOW = datetime.datetime(2026, 8, 4, 12, 0, 0, tzinfo=datetime.UTC)


def file(name: str, checksum: str = "aaa") -> MigrationFile:
    return MigrationFile(
        name=name,
        prefix=name[:17],
        path=pathlib.Path("/tmp") / name,
        sql="SELECT 1;",
        checksum=checksum,
        no_transaction=False,
    )


def row(name: str, checksum: str = "aaa") -> AppliedRow:
    return AppliedRow(name=name, checksum=checksum, applied_at=NOW)


class TestComputeStates:
    def test_file_with_matching_row_is_applied(self):
        states = compute_states([file("2026-08-04-091530-a.sql")], [row("2026-08-04-091530-a.sql")])
        assert [s.state for s in states] == [State.APPLIED]

    def test_file_without_a_row_is_pending(self):
        states = compute_states([file("2026-08-04-091530-a.sql")], [])
        assert [s.state for s in states] == [State.PENDING]

    def test_changed_checksum_is_drift(self):
        states = compute_states(
            [file("2026-08-04-091530-a.sql", checksum="new")],
            [row("2026-08-04-091530-a.sql", checksum="old")],
        )
        assert [s.state for s in states] == [State.DRIFT]

    def test_row_without_a_file_is_missing(self):
        states = compute_states([], [row("2026-08-04-091530-a.sql")])
        assert [s.state for s in states] == [State.MISSING]

    def test_missing_rows_are_sorted_in_among_the_files(self):
        states = compute_states(
            [file("2026-08-04-091530-b.sql"), file("2026-08-05-091530-c.sql")],
            [row("2026-07-29-000001-a.sql")],
        )
        assert [s.name for s in states] == [
            "2026-07-29-000001-a.sql",
            "2026-08-04-091530-b.sql",
            "2026-08-05-091530-c.sql",
        ]
        assert [s.state for s in states] == [State.MISSING, State.PENDING, State.PENDING]

    def test_carries_the_file_and_row_through(self):
        states = compute_states([file("2026-08-04-091530-a.sql")], [row("2026-08-04-091530-a.sql")])
        assert states[0].file is not None
        assert states[0].row is not None
        assert states[0].row.applied_at == NOW


class TestSelectors:
    def test_pending_returns_only_pending_in_order(self):
        states = compute_states(
            [file("2026-08-04-091530-a.sql"), file("2026-08-05-091530-b.sql")],
            [row("2026-08-04-091530-a.sql")],
        )
        assert [s.name for s in pending(states)] == ["2026-08-05-091530-b.sql"]

    def test_blocking_returns_drift_and_missing(self):
        states = compute_states(
            [file("2026-08-04-091530-a.sql", checksum="new")],
            [row("2026-08-04-091530-a.sql", checksum="old"), row("2026-07-29-000001-gone.sql")],
        )
        assert {s.state for s in blocking(states)} == {State.DRIFT, State.MISSING}

    def test_blocking_is_empty_when_everything_is_clean(self):
        states = compute_states([file("2026-08-04-091530-a.sql")], [row("2026-08-04-091530-a.sql")])
        assert blocking(states) == []
