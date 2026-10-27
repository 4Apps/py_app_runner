import sys
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Literal

from py_app_runner.db_pools import DbPools

_ENTRY_MODULES = ("_service", "_service_args", "_service_pybridge")


def _module_exists(module_name: str) -> bool:
    """Whether `module_name` can be imported, without executing it."""

    if module_name in sys.modules:
        return True

    try:
        return find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


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
        """A project package with any entry module owns the service name outright, so a
        built-in of the same name is never mixed in or imported. A project package without
        one (`services/bridge` holding only routes) extends the built-in instead."""

        project = f"services.{service_name}"
        found = self._import_or_none(f"{project}.{module}")
        if found is not None:
            return found

        if any(_module_exists(f"{project}.{entry}") for entry in _ENTRY_MODULES):
            return False

        found = self._import_or_none(f"py_app_runner.{service_name}.{module}")
        return found if found is not None else False

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
