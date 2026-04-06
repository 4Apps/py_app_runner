from collections.abc import Callable
from typing import Any


class AppRegistry:
    """
    Singleton registry holding project-specific classes and configuration
    that the framework needs. Must be configured before the runner starts.
    """

    _config: dict[str, Any] = {}
    _users_model_cls: type | None = None
    _api_keys_model_cls: type | None = None
    _audit_log_fn: Callable[..., Any] | None = None
    _redis_channel: str = ""
    _web_app_class: type | None = None

    @classmethod
    def configure(
        cls,
        *,
        config: dict[str, Any],
        users_model: type,
        api_keys_model: type,
        audit_log_fn: Callable[..., Any] | None = None,
        redis_channel: str = "",
        web_app_class: type | None = None,
    ) -> None:
        cls._config = config
        cls._users_model_cls = users_model
        cls._api_keys_model_cls = api_keys_model
        cls._audit_log_fn = audit_log_fn
        cls._redis_channel = redis_channel
        cls._web_app_class = web_app_class

    @classmethod
    def config(cls) -> dict[str, Any]:
        return cls._config

    @classmethod
    def users_model(cls) -> type:
        if cls._users_model_cls is None:
            raise RuntimeError("AppRegistry not configured: users_model is None")
        return cls._users_model_cls

    @classmethod
    def api_keys_model(cls) -> type:
        if cls._api_keys_model_cls is None:
            raise RuntimeError("AppRegistry not configured: api_keys_model is None")
        return cls._api_keys_model_cls

    @classmethod
    def audit_log_fn(cls) -> Callable[..., Any] | None:
        return cls._audit_log_fn

    @classmethod
    def redis_channel(cls) -> str:
        return cls._redis_channel

    @classmethod
    def web_app_class(cls) -> type | None:
        return cls._web_app_class
