import sys
import types

import pytest

from py_app_runner.pybridge import PyBridge


@pytest.fixture
def fake_modules():
    """Installs throwaway `services.*` / `py_app_runner.*` modules into sys.modules and
    removes them afterwards, so each test controls exactly which legs resolve."""

    created: list[str] = []

    def install(name: str, module: types.ModuleType | None = None) -> types.ModuleType:
        module = module if module is not None else types.ModuleType(name)
        sys.modules[name] = module
        created.append(name)
        return module

    yield install

    for name in reversed(created):
        sys.modules.pop(name, None)


def test_project_service_wins_over_builtin(fake_modules):
    fake_modules("services")
    fake_modules("services.demo")
    project = fake_modules("services.demo._service_args")
    project.origin = "project"
    fake_modules("py_app_runner.demo")
    builtin = fake_modules("py_app_runner.demo._service_args")
    builtin.origin = "builtin"

    assert PyBridge().load_service_args("demo").origin == "project"


def test_falls_back_to_builtin_when_project_module_absent(fake_modules):
    fake_modules("py_app_runner.demo")
    builtin = fake_modules("py_app_runner.demo._service_args")
    builtin.origin = "builtin"

    assert PyBridge().load_service_args("demo").origin == "builtin"


def test_returns_false_when_neither_leg_exists(fake_modules):
    assert PyBridge().load_service_args("nosuchservice") is False


def test_broken_import_inside_project_module_is_raised_not_swallowed(fake_modules, monkeypatch):
    """A project service whose own imports are broken must fail loudly. Falling through to
    the built-in here would silently run different code than the project asked for.

    `importlib.import_module` does not go through `builtins.__import__`, so the brief's
    monkeypatch-`__import__` approach never actually intercepts the call; this patches
    `pybridge.import_module` itself, which is what `_import_or_none` calls. The behaviour
    under test is unchanged: an import error naming a module OTHER than the one requested
    must propagate rather than being treated as "this leg doesn't exist"."""

    import py_app_runner.pybridge as pybridge_module

    real_import_module = pybridge_module.import_module

    def fake_import_module(name: str, *args, **kwargs):
        if name == "services.demo._service_args":
            raise ModuleNotFoundError(
                "No module named 'totally_absent_dependency'", name="totally_absent_dependency"
            )
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(pybridge_module, "import_module", fake_import_module)

    with pytest.raises(ModuleNotFoundError, match="totally_absent_dependency"):
        PyBridge().load_service_args("demo")
