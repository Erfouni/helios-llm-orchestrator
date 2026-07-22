#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"

exec /usr/bin/python3 "${PROJECT_ROOT}/scripts/refresh-benchmarks.py" --if-stale
