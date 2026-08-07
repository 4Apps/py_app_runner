"""Comparing a row before a change against the values written to it.

The comparison is the hard part. The two sides come from different places - one read back
from Postgres, one handed in by the caller - and psycopg returns real Python types, so
neither `==` nor a blind `str()` is right. `Decimal("10.50")` meets the `10.5` a caller
passed, and a `date` meets its ISO string; calling either pair different reports a change on
a column nobody touched, which is how an audit log becomes something nobody opens.
"""

import datetime
import json
from decimal import Decimal, InvalidOperation
from typing import Any

REDACTED = "***"

# Only consulted when at least one side is a real bool. That guard is load-bearing: without
# it a text column holding the literal "true" would silently equal one holding "1", and the
# trail would stop showing a change that really happened.
_TRUE = frozenset({"1", "t", "true", "y", "yes", "on"})
_FALSE = frozenset({"0", "f", "false", "n", "no", "off", ""})


def _boolish(value: Any) -> str:
    """Reduce a value to "1"/"0" if it reads as a boolean, else to something that cannot
    match either - so an unrecognised value still fails the comparison rather than being
    guessed at."""

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, int):
        return "1" if value == 1 else ("0" if value == 0 else str(value))

    text = str(value).strip().lower()
    if text in _TRUE:
        return "1"

    if text in _FALSE:
        return "0"

    return text


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, (int, float)):
        return Decimal(str(value))

    if isinstance(value, str):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None

    return None


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, datetime.date) and not isinstance(value, datetime.datetime)


def _as_datetime(value: Any, as_date: bool) -> datetime.datetime | datetime.date | None:
    """Parse to whichever of date/datetime the comparison is being made in.

    Which one that is depends on the *other* operand: a `date` column comes back from
    psycopg as a date, and its ISO string parsed as a datetime would be midnight on that
    day - never equal to the date itself. So the pair decides the type, not each side
    independently.
    """

    if isinstance(value, datetime.datetime):
        return value.date() if as_date else value

    if isinstance(value, datetime.date):
        return value

    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value) if as_date else datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    return None


def same(a: Any, b: Any) -> bool:
    """Whether two values represent the same stored content.

    Deliberately not `==`. The two sides come from different places - one read back from
    Postgres, one handed in by the caller - so a `date` legitimately meets its ISO string
    and a `Decimal` meets an int.
    """

    if a is None or b is None:
        return a is None and b is None

    if isinstance(a, bool) or isinstance(b, bool):
        return _boolish(a) == _boolish(b)

    # Ordered before the numeric branch: datetime is not a number, but date arithmetic
    # types would otherwise fall through to the string comparison and compare formatting
    # rather than instants.
    if isinstance(a, (datetime.datetime, datetime.date)) or isinstance(b, (datetime.datetime, datetime.date)):
        as_date = _is_plain_date(a) or _is_plain_date(b)
        left, right = _as_datetime(a, as_date), _as_datetime(b, as_date)
        if left is None or right is None:
            return str(a) == str(b)

        if isinstance(left, datetime.datetime) and isinstance(right, datetime.datetime):
            # A tz-aware value and a naive one cannot be ordered against each other, and
            # guessing a timezone for the naive side would silently shift it.
            if (left.tzinfo is None) != (right.tzinfo is None):
                return str(a) == str(b)

        return left == right

    if isinstance(a, (dict, list, tuple, set)) or isinstance(b, (dict, list, tuple, set)):
        return _json(a) == _json(b)

    left_number, right_number = _as_decimal(a), _as_decimal(b)
    if left_number is not None and right_number is not None:
        return left_number == right_number

    return str(a) == str(b)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def mask(values: dict[str, Any], exclude: list[str]) -> dict[str, Any]:
    """Replace excluded values while keeping their keys.

    The key stays so the trail still shows that a password changed; only the value is
    withheld.
    """

    if not exclude:
        return values

    return {key: (REDACTED if key in exclude else value) for key, value in values.items()}


def between(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    exclude: list[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Compare a row before and after, returning only what changed.

    Four shapes: (None, after) is an insert, (before, None) a delete, (before, after) an
    update reporting only differences, and (None, None) nothing.
    """

    excluded = exclude or []

    if before is None and after is None:
        return (None, None)

    if before is None:
        return (None, mask(after or {}, excluded))

    if after is None:
        return (mask(before, excluded), None)

    old_values: dict[str, Any] = {}
    new_values: dict[str, Any] = {}

    # Only keys present in `after` are considered. An update is routinely handed three
    # columns against a thirty-column row, and reporting the other twenty-seven as changes
    # to nothing is how a trail becomes unreadable.
    for key, new_value in after.items():
        # A key absent from `before` is a change from None - the case a naive dict diff
        # drops silently, and exactly the shape of "this column was just populated".
        old_value = before.get(key)

        if same(old_value, new_value):
            continue

        if key in excluded:
            old_values[key] = REDACTED
            new_values[key] = REDACTED
            continue

        old_values[key] = old_value
        new_values[key] = new_value

    if not new_values:
        # Collapses to a pair of Nones rather than empty dicts, which is what lets the
        # caller read "nothing changed" without inspecting the contents.
        return (None, None)

    return (old_values, new_values)
