import datetime as dt
import math
from decimal import Decimal
from enum import Enum
from typing import Any

import msgpack

from .base import MessageEncoder


def _msgpack_default(obj: Any) -> Any:
    """Custom packer for types msgpack doesn't handle natively."""
    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, dt.datetime):
        return obj.strftime("%Y-%m-%dT%H:%M:%S")

    if isinstance(obj, dt.date):
        return obj.strftime("%Y-%m-%d")

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return obj

    if isinstance(obj, (int, str)):
        return obj

    return str(obj)


class MsgpackEncoder(MessageEncoder):
    """MessagePack encoder for WebSocket binary frames."""

    def encode(self, data: dict[str, Any]) -> bytes:
        result: bytes = msgpack.packb(data, default=_msgpack_default, use_bin_type=True)  # type: ignore[assignment]
        return result

    def decode(self, raw: str | bytes) -> dict[str, Any]:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return msgpack.unpackb(raw, raw=False)

    @property
    def is_binary(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "msgpack"
