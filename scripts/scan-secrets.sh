#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

pattern='(sk-or-v1-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|WPL_''AP1\.[A-Za-z0-9._=-]{12,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----)'

if command -v rg >/dev/null 2>&1; then
  if rg -n --hidden --glob '!.git/**' --glob '!node_modules/**' --glob '!scripts/scan-secrets.sh' -e "$pattern" .; then
    echo "Potential secret detected." >&2
    exit 1
  fi
else
  if grep -REn --exclude-dir=.git --exclude-dir=node_modules --exclude=scan-secrets.sh "$pattern" .; then
    echo "Potential secret detected." >&2
    exit 1
  fi
fi

echo "Secret scan passed."
