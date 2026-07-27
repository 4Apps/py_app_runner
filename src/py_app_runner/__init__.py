from importlib.metadata import PackageNotFoundError, version

from py_app_runner.registry import AppRegistry

__all__ = ["AppRegistry"]

try:
    __version__ = version("py_app_runner")
except PackageNotFoundError:
    # Running straight from a source tree that was never installed
    __version__ = "0.0.dev0"
