import logging
import sys
from copy import deepcopy
from os import environ, getcwd, path
from typing import Any

from dotenv import load_dotenv


def coerce_value(value: Any) -> Any:
    """Turn a plain integer string into an int, leaving everything else alone.

    The round-trip check keeps values where the text form carries meaning - a zero
    padded id or a numeric secret must not silently become a different number.
    """
    if isinstance(value, list) or not isinstance(value, str):
        return value

    if value.isdigit() and str(int(value)) == value:
        return int(value)

    return value


def replace_rec(
    keys: list[str],
    finalDict: dict[str, Any],
    finalValue: Any,
) -> dict[str, Any]:
    key = keys.pop(0)
    if len(keys) == 0:
        finalDict[key] = coerce_value(finalValue)
        return finalDict

    if key not in finalDict:
        finalDict[key] = {}

    # An env var whose key path runs through an existing scalar cannot be merged
    # (e.g. API_KEY_PEPPER against a config that already has api.key). Overwriting
    # the scalar would silently destroy it, so refuse instead.
    if not isinstance(finalDict[key], dict):
        raise ValueError(f"Cannot descend into non-dict config key '{key}': it already holds a scalar value")

    finalDict[key] = replace_rec(keys, finalDict[key], finalValue)
    return finalDict


def parse_splitted_values(value: str, separator: str = ",") -> list[str]:
    valueList = value.split(separator)
    formattedList: list[str] = []
    for item in valueList:
        item = item.strip(" \r\n\t\\")
        if len(item) == 0 or item[0] == "#":
            continue

        formattedList.append(item)

    return formattedList


def is_env(env: str, config: dict[str, Any] | None = None) -> bool:
    if config is None:
        from py_app_runner.registry import AppRegistry

        config = AppRegistry.config()
    return env == config.get("environment", "")


def is_env_dev(config: dict[str, Any] | None = None) -> bool:
    return is_env("dev", config)


def is_env_test(config: dict[str, Any] | None = None) -> bool:
    return is_env("test", config)


def is_env_prod(config: dict[str, Any] | None = None) -> bool:
    return is_env("prod", config)


def load_config(
    defaults: dict[str, Any] | None = None,
    split_value_keys: list[str] | None = None,
) -> dict[str, Any]:
    """
    Load configuration from environment variables.

    1. Calls load_dotenv() from the current working directory
    2. Takes a `defaults` dict (the project's base config structure)
    3. Applies replace_rec() to fill in from env vars
    4. Handles split_value_keys (CSV splitting)
    5. Returns the populated dict
    """
    currentDirectory = getcwd()
    load_dotenv(path.join(currentDirectory, ".env"))

    appEnv = environ.get("APP_ENV", None)
    if not appEnv:
        logging.getLogger().error("*\n*\n* It seems that `.env` file is not loaded\n*\n*")
        sys.exit(-1)

    if defaults is None:
        defaults = {}

    if split_value_keys is None:
        split_value_keys = []

    # Deep copy: replace_rec writes into nested dicts, which a shallow copy would
    # share with the caller's (often module level) defaults.
    config_dict: dict[str, Any] = deepcopy(defaults)

    # Always set environment from APP_ENV
    config_dict["environment"] = appEnv

    for key in environ:
        keys = key.split("_")
        keys = [item.lower() for item in keys]
        value = environ[key]

        if len(keys) == 0 or config_dict.get(keys[0], None) is None:
            continue

        new_value: Any = parse_splitted_values(value) if key in split_value_keys else value

        try:
            config_dict = replace_rec(keys, config_dict, new_value)
        except ValueError as e:
            # One unmappable variable must not take the whole service down, but it
            # must be loud: the value the operator set is not in effect.
            logging.getLogger(__name__).error("Ignoring env var %s: %s", key, e)

    return config_dict
