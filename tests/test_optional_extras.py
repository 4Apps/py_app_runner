"""cron and crypto are extras. Without the library, importing the package must fail with
a message naming the extra to install, not a bare `ModuleNotFoundError` from three
frames deep."""

import subprocess
import sys

import pytest

CASES = [
    ("py_app_runner.cron", "cronsim", "cron"),
    ("py_app_runner.crypto", "cryptography", "crypto"),
    ("py_app_runner.crypto", "bcrypt", "crypto"),
]


def import_without(module: str, library: str) -> subprocess.CompletedProcess[str]:
    # `None` in sys.modules makes any import of that name raise ImportError, which is
    # what an uninstalled library does without uninstalling it from the test environment
    code = f"import sys; sys.modules[{library!r}] = None; import {module}"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(("module", "library", "extra"), CASES)
def test_missing_library_names_the_extra(module: str, library: str, extra: str):
    result = import_without(module, library)

    assert result.returncode != 0
    assert f"py_app_runner[{extra}]" in result.stderr


@pytest.mark.parametrize(("module", "library", "extra"), CASES)
def test_installed_library_imports_cleanly(module: str, library: str, extra: str):
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
