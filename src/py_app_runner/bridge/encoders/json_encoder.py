from typing import Any

from py_app_runner.utils import json_decode, json_encode

from .base import MessageEncoder


class JsonEncoder(MessageEncoder):
    """JSON message encoder for WebSocket frames."""

    def encode(self, data: dict[str, Any]) -> str:
        return json_encode(data)

    def decode(self, raw: str | bytes) -> dict[str, Any]:
        try:
            return json_decode(raw)
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    @property
    def is_binary(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return "json"
