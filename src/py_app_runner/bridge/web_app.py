from typing import Any

from tornado.web import url as TornadoUrl

from py_app_runner.bridge.api import ApiHandler
from py_app_runner.bridge.websocket import MsgpackWebSocketHandler, WebSocketHandler
from py_app_runner.config import is_env_dev
from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import WebApplicationBase


class WebApplication(WebApplicationBase):
    """Tornado web application expansion to initialize stuff like database connections and/or sessions"""

    def get_extra_handlers(self, handler_kwargs: dict[str, Any]) -> list[Any]:
        """Override in subclasses to add project-specific handlers."""
        return []

    def __init__(self, **kwargs: Any):
        handlerKwargs: dict[str, Any] = {
            "version": 1,
            "api_key": None,
            "api_key_use_db": True,
        }
        handlers = [
            TornadoUrl(
                r"/v1/json/([^/]+)(?:/([^/]+))?",
                ApiHandler,
                kwargs=handlerKwargs,
                name="v1-api",
            ),
            TornadoUrl(
                r"/v1/socket",
                WebSocketHandler,
                kwargs=handlerKwargs,
                name="v1-websocket",
            ),
            TornadoUrl(
                r"/v1/socket/msgpack",
                MsgpackWebSocketHandler,
                kwargs=handlerKwargs,
                name="v1-websocket-msgpack",
            ),
        ]

        # Allow subclasses to add extra handlers
        extra = self.get_extra_handlers(handlerKwargs)
        if extra:
            handlers.extend(extra)

        kwargs["handlers"] = handlers
        kwargs["debug"] = is_env_dev(AppRegistry.config())

        # Init parent
        super().__init__(**kwargs)
