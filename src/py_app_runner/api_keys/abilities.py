"""Ability strings: `service:action`, `service:*` or `*`.

The service part is normalised the way `find_service` normalises a request's service name
(`-` becomes `_`), so `sudat-reports:report` and `sudat_reports:report` are one ability.
"""

from collections.abc import Iterable

WILDCARD = "*"


def normalize_ability(raw: str) -> str:
    ability = raw.strip()
    if ability == WILDCARD:
        return ability

    service, sep, action = ability.partition(":")
    if not sep or not service or not action or ":" in action:
        raise ValueError(f"ability {raw!r} must be 'service:action', 'service:*' or '*'")

    if WILDCARD in service or (WILDCARD in action and action != WILDCARD):
        raise ValueError(f"ability {raw!r}: '*' may only stand for a whole action or for everything")

    if any(c.isspace() for c in ability):
        raise ValueError(f"ability {raw!r} contains whitespace")

    return f"{service.replace('-', '_')}:{action}"


def ability_allows(granted: Iterable[str] | None, ability: str) -> bool:
    """Whether `granted` covers `ability`. Nothing granted allows nothing."""

    service, _, action = ability.partition(":")
    service = service.replace("-", "_")
    accepted = {WILDCARD, f"{service}:{action}", f"{service}:{WILDCARD}"}

    return any(item in accepted for item in granted or ())
