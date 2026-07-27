from typing import Any

import pytest

from py_app_runner.config import coerce_value, load_config, replace_rec


class TestCoerceValue:
    def test_plain_integers_become_ints(self):
        assert coerce_value("5432") == 5432
        assert coerce_value("0") == 0

    def test_values_whose_text_form_matters_stay_strings(self):
        # A zero padded id or a numeric secret must survive intact.
        assert coerce_value("0071234") == "0071234"
        assert coerce_value("007") == "007"

    def test_non_strings_pass_through(self):
        assert coerce_value(["a", "b"]) == ["a", "b"]
        assert coerce_value(None) is None


class TestReplaceRec:
    def test_creates_missing_nested_keys(self):
        config: dict[str, Any] = {"db": {}}
        replace_rec(["db", "main", "host"], config, "example.com")
        assert config == {"db": {"main": {"host": "example.com"}}}

    def test_refuses_to_descend_into_a_scalar(self):
        # e.g. API_KEY_PEPPER against a config that already holds api.key
        config: dict[str, Any] = {"api": {"key": "SECRET"}}
        with pytest.raises(ValueError):
            replace_rec(["api", "key", "pepper"], config, "pepper-value")
        assert config["api"]["key"] == "SECRET"


class TestLoadConfig:
    def test_defaults_are_not_mutated(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("DB_MAIN_HOST", "prod-host")

        defaults: dict[str, Any] = {"db": {"main": {"host": "localhost"}}}
        config = load_config(defaults=defaults)

        assert config["db"]["main"]["host"] == "prod-host"
        assert defaults["db"]["main"]["host"] == "localhost"

    def test_unmappable_env_var_is_skipped_not_fatal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("API_KEY", "the-key")
        monkeypatch.setenv("API_KEY_PEPPER", "the-pepper")

        config = load_config(defaults={"api": {"key": ""}})

        assert config["api"]["key"] == "the-key"

    def test_split_value_keys_produce_lists(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("SERVICES", "bridge, worker")

        config = load_config(defaults={"services": []}, split_value_keys=["SERVICES"])

        assert config["services"] == ["bridge", "worker"]
