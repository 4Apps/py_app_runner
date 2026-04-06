#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONWARNINGS=error

source /srv/meta/scripts/console.bash || true

# Go to app base
cd /srv/app

# Strict byte-compile (syntax check)
echo_process "Byte-compiling Python sources... "
if ! python -W error -m compileall -q -f -j0 src; then
  echo_fail "!!! ERROR: Python compile failed!"
  exit 1
fi
echo_ok ""

# Import smoke test
echo_process "Importing package for smoke test... "
python -c "from py_app_runner import AppRegistry; from py_app_runner.runner import main"
echo_ok ""

# Style check with ruff
echo_process "Running ruff style checks... "
ruff check src/
echo_ok ""

# Static type check with pyrefly
echo_process "Running pyrefly static type checks... "
pyrefly check src/
echo_ok ""

# Dependency graph sanity
echo_process "Checking installed package dependencies... "
pip check > /dev/null
echo_ok ""

echo_success "All tests passed."
