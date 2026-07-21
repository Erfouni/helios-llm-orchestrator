#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
exec /usr/bin/env python3 "${PROJECT_ROOT}/agent/server.py"
