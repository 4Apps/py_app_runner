"""Scoped API keys: the record, ability matching and key material. The CLI lives in
`_service.py` and is enabled by listing `api_keys` in SERVICES."""

from py_app_runner.api_keys.abilities import ability_allows, normalize_ability
from py_app_runner.api_keys.model import ApiKeysModel

__all__ = ["ApiKeysModel", "ability_allows", "normalize_ability"]
