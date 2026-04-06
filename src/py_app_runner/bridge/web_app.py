from typing import Any

from tornado.web import url as TornadoUrl

from py_app_runner.config import is_env_dev
from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import WebApplicationBase

RouteDefinition = tuple[str, type, str]


class WebApplication(WebApplicationBase):
    """Tornado web application that builds its handler list from a project-defined routes list."""

    def __init__(self, routes: list[RouteDefinition] | None = None, **kwargs: Any):
        handlers = []
        if routes:
            for pattern, handler_class, name in routes:
                handlers.append(TornadoUrl(pattern, handler_class, name=name))

        kwargs["handlers"] = handlers
        kwargs["debug"] = is_env_dev(AppRegistry.config())

        super().__init__(**kwargs)
