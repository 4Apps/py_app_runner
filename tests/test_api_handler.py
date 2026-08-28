from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from py_app_runner.http_exception import HTTPException
from py_app_runner.registry import AppRegistry
from py_app_runner.utils import json_encode

from .api_harness import FakeService, make_api_app

API_KEY = "top-secret-key"


class TestApiHandlerInput(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        AppRegistry.configure(
            config={"environment": "test", "debug": False, "api": {"key": API_KEY}, "jwt": {"secret": "jwt-secret"}},
            users_model=object,
            api_keys_model=object,
        )
        self.echo = FakeService(result={"status": "ok"})
        self.guarded = FakeService(result={"status": "ok"}, requires_api_key=None)
        self.rejecting = FakeService(error=HTTPException("bad credentials", code=-401, http_status=401))
        return make_api_app({"echo": self.echo, "guarded": self.guarded, "rejecting": self.rejecting})

    def test_credentials_never_reach_the_logs(self):
        # Every HTTPException logs the headers and the full body at WARNING, so a failed
        # login would otherwise write the password and the caller's tokens to the log.
        body = {
            "action": "login",
            "auth_token": "token-in-body",
            "data": {"email": "a@b.c", "password": "hunter2"},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer token-in-authorization",
            "X-Auth-Token": "token-in-custom-header",
            "X-Api-Secret": "secret-in-custom-header",
            "X-API-Key": API_KEY,
        }
        with self.assertLogs("py_app_runner", level="DEBUG") as captured:
            response = self.fetch("/api/rejecting", method="POST", body=json_encode(body), headers=headers)

        assert response.code == 401
        logged = "\n".join(captured.output)
        for secret in (
            "hunter2",
            "token-in-body",
            "token-in-authorization",
            "token-in-custom-header",
            "secret-in-custom-header",
            API_KEY,
        ):
            assert secret not in logged, secret
        # Redaction is targeted, not a blanket suppression of the body.
        assert "login" in logged

    def test_api_key_is_read_from_the_header_only(self):
        # A key in the query string lands in proxy access logs and Referer headers.
        assert self.fetch(f"/api/guarded?action=go&api_key={API_KEY}").code == 401
        assert self.fetch("/api/guarded?action=go", headers={"X-API-Key": API_KEY}).code == 200

    def test_query_parameters_are_not_service_input(self):
        # Only the JSON body feeds the service. A `data` from the URL is a bare string
        # that no handler expecting a dict can use, and URLs get logged and cached.
        response = self.fetch("/api/echo?action=go&data=from-url&extra=1")

        assert response.code == 200
        assert self.echo.calls == [("go", {})]
