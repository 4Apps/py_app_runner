from abc import ABC, abstractmethod
from asyncio import AbstractEventLoop, Future
from typing import Any


class WebSocketHandlerInterface(ABC):
    uid: str | None
    msgId: int | None
    service: str | None
    current_session_sid: str | None

    loop: AbstractEventLoop

    @abstractmethod
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.uid = None
        self.msgId = None
        self.service = None
        self.current_session_sid = None

    @abstractmethod
    def error_message(self, msg: str, code: int = -1, **kwargs: Any) -> None:
        pass

    @abstractmethod
    def write_custom_message(
        self,
        message: bytes | str | dict[str, Any] | list[Any],
        wrap_in_data: bool = True,
        **kwargs: Any,
    ) -> Future[None]:
        pass

    @abstractmethod
    def check_origin(self, origin: str) -> bool:
        pass

    @abstractmethod
    async def open(self, *args: str, **kwargs: str) -> None:
        pass

    @abstractmethod
    def ping(self, data: str | bytes = b"") -> None:
        pass

    @abstractmethod
    async def on_message(self, message: str | bytes) -> None:
        pass

    @abstractmethod
    def close(self, code: int | None = None, reason: str | None = None) -> None:
        pass

    @abstractmethod
    def on_close(self) -> None:
        pass
