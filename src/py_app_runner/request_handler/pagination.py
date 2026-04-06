from typing import Any

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100


def parse_pagination(input_data: dict[str, Any] | None, default_limit: int = DEFAULT_PAGE_LIMIT) -> tuple[int, int]:
    """Extract and clamp limit/offset from client input."""
    if not input_data:
        return default_limit, 0

    try:
        limit = int(input_data.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit

    try:
        offset = int(input_data.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    offset = max(0, offset)
    return limit, offset
