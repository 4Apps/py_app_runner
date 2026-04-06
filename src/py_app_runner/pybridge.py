from importlib import import_module
from typing import Any, Literal

from py_app_runner.db_pools import DbPools


class PyBridge:
    services_placeholder: dict[str, Any] = {}
    db_pools: DbPools

    def __init__(self) -> None: ...

    def load_service(self, service_name: str) -> Any:
        try:
            service = import_module(f"services.{service_name}._service")
        except ModuleNotFoundError as _e:
            service = import_module(f"services.{service_name}")

        return service

    def load_service_args(self, service_name: str) -> Any | Literal[False]:
        load_service_name = f"services.{service_name}._service_args"
        try:
            return import_module(load_service_name)
        except ModuleNotFoundError as _e:
            if load_service_name in _e.msg:
                return False

            raise

    def load_service_runner(self, service_name: str) -> Any | Literal[False]:
        load_service_name = f"services.{service_name}._service"
        try:
            return import_module(load_service_name)
        except ModuleNotFoundError as e:
            if load_service_name in e.msg:
                return False

            raise

    def load_service_pybridge(self, service_name: str) -> Any | Literal[False]:
        load_service_name = f"services.{service_name}._service_pybridge"
        try:
            return import_module(load_service_name)
        except ModuleNotFoundError as _e:
            if load_service_name in _e.msg:
                return False

            raise

    def cache_service(self, service_name: str, service: Any) -> None:
        self.services_placeholder[service_name] = service

    def get_service(self, service_name: str) -> Any | Literal[False]:
        return self.services_placeholder.get(service_name, False)
