from typing import Any

from py_app_runner.registry import AppRegistry
from py_app_runner.request_handler.handlers import RequestHandlerApiKeys


def check_key(config: dict[str, Any], api_key: str) -> Any:
    AppRegistry.configure(config=config, users_model=object, api_keys_model=object)
    return RequestHandlerApiKeys.has_valid_key(object(), api_key)  # type: ignore[arg-type]


class TestStaticApiKey:
    def test_single_key_match(self):
        assert check_key({"api": {"key": "secret"}}, "secret") is True
        assert isinstance(check_key({"api": {"key": "secret"}}, "wrong"), str)

    def test_list_of_keys(self):
        assert check_key({"api": {"key": ["a", "b"]}}, "b") is True
        assert isinstance(check_key({"api": {"key": ["a", "b"]}}, "c"), str)

    def test_non_ascii_key_is_rejected_not_crashing(self):
        # compare_digest raises TypeError on non-ASCII str, and the value is supplied
        # by the client.
        assert isinstance(check_key({"api": {"key": "secret"}}, "sécret"), str)

    def test_missing_configuration(self):
        assert isinstance(check_key({"api": {}}, "anything"), str)
