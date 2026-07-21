#!/bin/zsh
set -euo pipefail

SERVICE="${OPENROUTER_KEYCHAIN_SERVICE:-helios-multimodel-router}"
ACCOUNT="${OPENROUTER_KEYCHAIN_ACCOUNT:-openrouter-api-key}"

printf "OpenRouter API key: "
IFS= read -r -s API_KEY
printf "\n"

if [[ -z "${API_KEY}" ]]; then
  echo "No key entered; Keychain was not changed." >&2
  exit 1
fi

security add-generic-password -U -s "${SERVICE}" -a "${ACCOUNT}" -w "${API_KEY}" >/dev/null
unset API_KEY

echo "OpenRouter credential saved to macOS Keychain."
