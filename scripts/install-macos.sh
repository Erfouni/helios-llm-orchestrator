#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
# These labels intentionally retain the legacy names for upgrade compatibility.
LABEL="com.local.helios-multimodel-router"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${PROJECT_ROOT}/deploy/${LABEL}.plist.template"
BENCHMARK_LABEL="com.local.helios-benchmark-refresh"
BENCHMARK_PLIST="${HOME}/Library/LaunchAgents/${BENCHMARK_LABEL}.plist"
BENCHMARK_TEMPLATE="${PROJECT_ROOT}/deploy/${BENCHMARK_LABEL}.plist.template"

PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
NODE_BIN="$(command -v node 2>/dev/null || true)"
NPM_BIN="$(command -v npm 2>/dev/null || true)"

[[ -n "${PYTHON_BIN}" ]] || { echo "Python 3.10+ is required." >&2; exit 1; }
[[ -n "${NODE_BIN}" ]] || { echo "Node.js 22+ is required." >&2; exit 1; }
[[ -n "${NPM_BIN}" ]] || { echo "npm is required." >&2; exit 1; }

"${PYTHON_BIN}" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required; found " + sys.version.split()[0])
PY

NODE_MAJOR="$("${NODE_BIN}" -p 'Number(process.versions.node.split(".")[0])')"
if (( NODE_MAJOR < 22 )); then
  echo "Node.js 22+ is required; found $("${NODE_BIN}" --version)." >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
export HELIOS_PYTHON_BIN="${PYTHON_BIN}"
"${NPM_BIN}" ci
"${NPM_BIN}" test
"${NPM_BIN}" run scan:secrets
"${NPM_BIN}" run audit:prod

if ! security find-generic-password -s "helios-multimodel-router" -a "openrouter-api-key" -w >/dev/null 2>&1; then
  "${PROJECT_ROOT}/scripts/configure-key.sh"
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${PROJECT_ROOT}/logs"
sed -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" "${TEMPLATE}" > "${PLIST}"
sed -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" "${BENCHMARK_TEMPLATE}" > "${BENCHMARK_PLIST}"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

launchctl bootout "gui/$(id -u)/${BENCHMARK_LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${BENCHMARK_PLIST}"

if ! "${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/refresh-benchmarks.py" --if-stale; then
  echo "Warning: initial benchmark refresh failed; the agent is installed and the weekly scheduler will retry." >&2
fi

echo "Helios installed. Health: http://127.0.0.1:3188/health"
echo "Weekly public benchmark refresh scheduled for Monday at 03:00 local time."
