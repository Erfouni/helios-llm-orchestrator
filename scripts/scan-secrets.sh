#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

pattern='(sk-or-v1-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|WPL_''AP1\.[A-Za-z0-9._=-]{12,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----)'

if command -v rg >/dev/null 2>&1 && rg --version >/dev/null 2>&1; then
  set +e
  rg -n --hidden --glob '!.git/**' --glob '!node_modules/**' --glob '!scripts/scan-secrets.sh' -e "$pattern" .
  scan_status=$?
  set -e
else
  set +e
  grep -REn --exclude-dir=.git --exclude-dir=node_modules --exclude=scan-secrets.sh "$pattern" .
  scan_status=$?
  set -e
fi

if [ "$scan_status" -eq 0 ]; then
  echo "Potential secret detected." >&2
  exit 1
elif [ "$scan_status" -ne 1 ]; then
  echo "Secret scan failed with status $scan_status." >&2
  exit "$scan_status"
fi

echo "Secret scan passed."
