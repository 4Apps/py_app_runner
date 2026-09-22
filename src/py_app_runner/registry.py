from typing import Any

from py_app_runner.api_keys.model import ApiKeysModel


class AppRegistry:
    """
    Singleton registry holding project-specific classes and configuration
    that the framework needs. Must be configured before the runner starts.
    """

    _config: dict[str, Any] = {}
    _users_model_cls: type | None = None
    _api_keys_model_cls: type = ApiKeysModel
    _redis_channel: str = ""
    _api_key_use_db: bool = False

    @classmethod
    def configure(
        cls,
        *,
        config: dict[str, Any],
        users_model: type,
        api_keys_model: type | None = None,
        redis_channel: str = "",
        api_key_use_db: bool = False,
    ) -> None:
        cls._config = config
        cls._users_model_cls = users_model
        cls._api_keys_model_cls = api_keys_model or ApiKeysModel
        cls._redis_channel = redis_channel
        cls._api_key_use_db = api_key_use_db

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
        return cls._api_keys_model_cls

    @classmethod
    def redis_channel(cls) -> str:
        return cls._redis_channel

    @classmethod
    def api_key_use_db(cls) -> bool:
        return cls._api_key_use_db
