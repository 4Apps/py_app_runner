from typing import Any

from tornado.web import Application

from py_app_runner.bridge.api import ApiHandler
from py_app_runner.pybridge import PyBridge
from py_app_runner.request_handler.handlers import WebApplicationBase


class FakeService:
    """Stands in for a services.<name>._service_pybridge module and records what it received."""

    def __init__(
        self,
        result: Any = None,
        error: Exception | None = None,
        requires_api_key: bool | None = False,
    ):
        self.result = result
        self.error = error
        self.requires_api_key = requires_api_key
        self.calls: list[tuple[str, Any]] = []

    async def bridge_request(self, action: str, request_data: dict[str, Any], bridge_handler: Any) -> Any:
        self.calls.append((action, request_data))
        if self.error is not None:
            raise self.error
        return self.result


def make_api_app(services: dict[str, FakeService]) -> Application:
    class ApiApplication(WebApplicationBase):
        pass

    app = ApiApplication([(r"/api/([^/]+)", ApiHandler)])

    pybridge = PyBridge()
    for name, service in services.items():
        pybridge.cache_service(name, service)
    app.set_pybridge(pybridge)

    return app
