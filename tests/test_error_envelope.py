from typing import Any

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from py_app_runner.http_exception import HTTPException
from py_app_runner.registry import AppRegistry
from py_app_runner.utils import json_decode


def make_app() -> Application:
    from py_app_runner.request_handler.handlers import WebHandlerBase

    class StringError(WebHandlerBase):
        async def get(self) -> None:  # type: ignore[override]
            self.error("plain message", -42, 418)

    class ExceptionError(WebHandlerBase):
        async def get(self) -> None:  # type: ignore[override]
            self.error(HTTPException("not found", code=-404, http_status=404))

    class Success(WebHandlerBase):
        async def get(self) -> None:  # type: ignore[override]
            self.write({"status": "ok"})

    return Application(
        [
            (r"/string-error", StringError),
            (r"/exception-error", ExceptionError),
            (r"/success", Success),
        ]
    )


class FakeService:
    """Stands in for a services.<name>._service_pybridge module."""

    requires_api_key = False

    def __init__(self, result: Any = None, error: Exception | None = None):
        self.result = result
        self.error = error

    async def bridge_request(self, action: str, request_data: dict[str, Any], bridge_handler: Any) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


def make_api_app() -> Application:
    from py_app_runner.bridge.api import ApiHandler
    from py_app_runner.pybridge import PyBridge
    from py_app_runner.request_handler.handlers import WebApplicationBase

    class ApiApplication(WebApplicationBase):
        pass

    app = ApiApplication([(r"/api/([^/]+)", ApiHandler)])

    pybridge = PyBridge()
    pybridge.cache_service("boom", FakeService(error=RuntimeError("service exploded")))
    pybridge.cache_service("known", FakeService(error=HTTPException("nope", code=-7, http_status=403)))
    pybridge.cache_service("fine", FakeService(result={"status": "ok"}))
    pybridge.cache_service("silent", FakeService(result=None))
    app.set_pybridge(pybridge)

    return app


class TestApiHandlerStatus(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        AppRegistry.configure(
            config={"environment": "test", "debug": False},
            users_model=object,
            api_keys_model=object,
        )
        return make_api_app()

    def test_unexpected_exception_is_a_server_error(self):
        # A crash inside a service is our fault: a 4xx would tell the client to stop
        # retrying and would hide the failure from monitoring.
        response = self.fetch("/api/boom?action=go")
        assert response.code == 500
        assert json_decode(response.body)["data"]["error"]["code"] == -1

    def test_http_exception_keeps_its_own_status(self):
        response = self.fetch("/api/known?action=go")
        assert response.code == 403
        assert json_decode(response.body) == {"data": {"error": {"msg": "nope", "code": -7}}}

    def test_success_is_unaffected(self):
        response = self.fetch("/api/fine?action=go")
        assert response.code == 200
        assert json_decode(response.body) == {"data": {"status": "ok"}}

    def test_service_returning_nothing_is_no_content(self):
        # Not an error: the WebSocket path stays silent for the same case.
        response = self.fetch("/api/silent?action=go")
        assert response.code == 204
        assert response.body == b""
        assert "Content-Type" not in response.headers


class TestErrorEnvelope(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        AppRegistry.configure(
            config={"environment": "test", "debug": False},
            users_model=object,
            api_keys_model=object,
        )
        return make_app()

    def _body(self, path: str) -> tuple[int, Any]:
        response = self.fetch(path)
        return response.code, json_decode(response.body)

    def test_string_error_is_wrapped_in_data(self):
        # `data` is the sole payload container - HTTP and WebSocket clients must be
        # able to parse errors the same way.
        code, body = self._body("/string-error")
        assert code == 418
        assert body == {"data": {"error": {"code": -42, "msg": "plain message"}}}

    def test_http_exception_is_wrapped_in_data(self):
        code, body = self._body("/exception-error")
        assert code == 404
        assert body == {"data": {"error": {"msg": "not found", "code": -404}}}

    def test_success_is_wrapped_in_data(self):
        code, body = self._body("/success")
        assert code == 200
        assert body == {"data": {"status": "ok"}}
