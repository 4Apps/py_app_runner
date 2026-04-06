from typing import Any


class HTTPException(Exception):
    message: str
    code: int
    description: str | None
    http_status: int
    logged: bool

    def __init__(
        self,
        message: str,
        code: int = 1,
        description: str | None = None,
        http_status: int = 400,
        logged: bool = False,
    ) -> None:
        super().__init__(message)

        self.message = message
        self.code = code
        self.description = description
        self.http_status = http_status
        self.logged = logged

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"msg": self.message, "code": self.code}
        if self.description:
            result["description"] = self.description
        return result
