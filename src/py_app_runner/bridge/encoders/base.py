from abc import ABC, abstractmethod
from typing import Any


class MessageEncoder(ABC):
    """Abstract base for WebSocket message encoding/decoding."""

    @abstractmethod
    def encode(self, data: dict[str, Any]) -> str | bytes:
        """Encode a dict into a wire format (str for text frames, bytes for binary)."""

    @abstractmethod
    def decode(self, raw: str | bytes) -> dict[str, Any]:
        """Decode a raw WebSocket message into a dict."""

    @property
    @abstractmethod
    def is_binary(self) -> bool:
        """Whether this encoder produces binary frames (True) or text frames (False)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""
