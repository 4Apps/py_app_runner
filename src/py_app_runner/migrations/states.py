from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from py_app_runner.migrations.discovery import MigrationFile


class State(StrEnum):
    APPLIED = "applied"
    PENDING = "PENDING"
    DRIFT = "DRIFT"
    MISSING = "MISSING"


@dataclass(frozen=True)
class AppliedRow:
    name: str
    checksum: str
    applied_at: datetime


@dataclass(frozen=True)
class MigrationState:
    name: str
    state: State
    file: MigrationFile | None
    row: AppliedRow | None


def compute_states(files: list[MigrationFile], rows: list[AppliedRow]) -> list[MigrationState]:
    """Union of disk and table, ordered by name. A row with no file (MISSING) sorts in
    among the files rather than being appended, so `status` reads chronologically."""

    by_name = {file.name: file for file in files}
    rows_by_name = {row.name: row for row in rows}

    states = []
    for name in sorted(by_name.keys() | rows_by_name.keys()):
        file = by_name.get(name)
        row = rows_by_name.get(name)

        if file is None:
            state = State.MISSING
        elif row is None:
            state = State.PENDING
        elif row.checksum != file.checksum:
            state = State.DRIFT
        else:
            state = State.APPLIED

        states.append(MigrationState(name=name, state=state, file=file, row=row))

    return states


def pending(states: list[MigrationState]) -> list[MigrationState]:
    return [state for state in states if state.state is State.PENDING]


def blocking(states: list[MigrationState]) -> list[MigrationState]:
    """States that must be resolved before anything may be applied."""

    return [state for state in states if state.state in (State.DRIFT, State.MISSING)]
