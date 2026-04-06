from typing import Any, Generic, TypeVar

T = TypeVar("T")


class StatusModel:
    status: str
    message: str | None

    def __init__(self, status: str = "ok", message: str | None = None):
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return str(self.to_dict())

    def __repr__(self) -> str:
        return self.__str__()

    def to_dict(self) -> dict[str, Any]:
        newDict = {"status": self.status}
        if self.message:
            newDict["message"] = self.message
        return newDict


class MessageModel:
    """
    MessageModel
    """

    code: int
    text: str

    def __init__(self, text: str, code: int = 0):
        self.text = text
        self.code = code

    def __str__(self) -> str:
        return f"code: {self.code} | message: {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "text": self.text,
        }


class ReturnModel(Generic[T]):
    """
    ReturnModel
    """

    result: T | None
    error: MessageModel | None

    # Additional info
    info: Any | None

    def __init__(
        self,
        result: T | None = None,
        error: MessageModel | None = None,
        info: Any | None = None,
    ):
        self.result = result
        self.error = error
        self.info = info

    def __str__(self) -> str:
        return f"result: {self.result} | error message: {self.error} | info: {self.info}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "error": self.error.to_dict() if self.error else None,
            "info": self.info,
        }
