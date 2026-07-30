from importlib import import_module
from typing import Any, Literal

from py_app_runner.db_pools import DbPools


class PyBridge:
    services_placeholder: dict[str, Any]
    db_pools: DbPools

    def __init__(self) -> None:
        self.services_placeholder = {}

    def _import_or_none(self, module_name: str) -> Any | None:
        """Imports `module_name`, returning None only when that module - or one of its own
        ancestor packages - is the thing that is missing. An ImportError naming anything
        else is a broken import *inside* the module and must surface - swallowing it would
        let a typo in a project service silently fall through to the built-in of the same
        name."""

        try:
            return import_module(module_name)
        except ModuleNotFoundError as e:
            if e.name is not None and (e.name == module_name or module_name.startswith(f"{e.name}.")):
                return None

            raise

    def _resolve(self, service_name: str, module: str) -> Any | Literal[False]:
        """Project services win over built-ins, so a project can override `bridge` or
        `migrations` by shipping its own `services.<name>.<module>`."""

        for package in (f"services.{service_name}", f"py_app_runner.{service_name}"):
            found = self._import_or_none(f"{package}.{module}")
            if found is not None:
                return found

        return False

    def load_service(self, service_name: str) -> Any:
        service = self._resolve(service_name, "_service")
        if service is not False:
            return service

        return import_module(f"services.{service_name}")

    def load_service_args(self, service_name: str) -> Any | Literal[False]:
        return self._resolve(service_name, "_service_args")

    def load_service_runner(self, service_name: str) -> Any | Literal[False]:
        return self._resolve(service_name, "_service")

    def load_service_pybridge(self, service_name: str) -> Any | Literal[False]:
        return self._resolve(service_name, "_service_pybridge")

    def cache_service(self, service_name: str, service: Any) -> None:
        self.services_placeholder[service_name] = service

    def register_alias(self, name: str, handler: Any) -> None:
        """Expose an already-loaded handler under an additional service name."""
        self.services_placeholder[name] = handler

    def get_service(self, service_name: str) -> Any | Literal[False]:
        return self.services_placeholder.get(service_name, False)
