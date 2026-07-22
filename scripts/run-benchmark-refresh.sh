#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
PYTHON_BIN="${HELIOS_PYTHON_BIN:-$(command -v python3)}"
exec "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/refresh-benchmarks.py" --if-stale
