#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
LABEL="com.local.helios-multimodel-router"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${PROJECT_ROOT}/deploy/${LABEL}.plist.template"
BENCHMARK_LABEL="com.local.helios-benchmark-refresh"
BENCHMARK_PLIST="${HOME}/Library/LaunchAgents/${BENCHMARK_LABEL}.plist"
BENCHMARK_TEMPLATE="${PROJECT_ROOT}/deploy/${BENCHMARK_LABEL}.plist.template"

command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
command -v node >/dev/null || { echo "Node.js 22+ is required." >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is required." >&2; exit 1; }

cd "${PROJECT_ROOT}"
npm ci
npm test
npm run scan:secrets

if ! security find-generic-password -s "helios-multimodel-router" -a "openrouter-api-key" -w >/dev/null 2>&1; then
  "${PROJECT_ROOT}/scripts/configure-key.sh"
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${PROJECT_ROOT}/logs"
sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${TEMPLATE}" > "${PLIST}"
sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${BENCHMARK_TEMPLATE}" > "${BENCHMARK_PLIST}"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

launchctl bootout "gui/$(id -u)/${BENCHMARK_LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${BENCHMARK_PLIST}"

echo "Helios installed. Health: http://127.0.0.1:3188/health"
echo "Weekly public benchmark refresh scheduled for Monday at 03:00 local time."
