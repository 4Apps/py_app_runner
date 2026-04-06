import logging
import sys
from os import environ, getcwd, path
from typing import Any

from dotenv import load_dotenv


def replace_rec(
    keys: list[str],
    finalDict: dict[str, Any],
    finalValue: Any,
) -> dict[str, Any]:
    key = keys.pop(0)
    if len(keys) == 0:
        if not isinstance(finalValue, list) and finalValue.isdigit():
            finalDict[key] = int(finalValue)
        else:
            finalDict[key] = finalValue
        return finalDict

    if key not in finalDict:
        finalDict[key] = {}

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

    config_dict: dict[str, Any] = dict(defaults)

    # Ensure environment is set from APP_ENV
    if "environment" not in config_dict:
        config_dict["environment"] = appEnv

    for key in environ:
        keys = key.split("_")
        keys = [item.lower() for item in keys]
        value = environ[key]

        if len(keys) == 0 or config_dict.get(keys[0], None) is None:
            continue

        if key in split_value_keys:
            newValue = parse_splitted_values(value)
            config_dict = replace_rec(keys, config_dict, newValue)
            continue

        config_dict = replace_rec(keys, config_dict, value)

    return config_dict
