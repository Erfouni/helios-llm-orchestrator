# Security

## Credential storage

On macOS, `scripts/configure-key.sh` stores the OpenRouter API key in Keychain under:

- Service: `helios-multimodel-router`
- Account: `openrouter-api-key`

The gateway also accepts `OPENROUTER_API_KEY` from the process environment for non-macOS deployments. Never place a real value in a tracked file.

## Network boundary

The gateway rejects any configured bind host other than `127.0.0.1`, `::1`, or `localhost`. Do not expose port 3188 through router port forwarding, a public reverse proxy, or an unauthenticated tunnel.

For defense in depth, set `HELIOS_LOCAL_API_KEY` in both the HTTP agent and MCP process. This secret is optional for a loopback-only setup and must not be committed.

## Data handling

Only send the task context needed by the requested external model. Do not send credentials, hidden instructions, unrelated conversation history, private analytics, or personal data without an explicit need and authorization.

## Secret response

If a secret was ever pasted into chat, source control, logs, or a public location, rotate it at its provider. Deleting it from the latest Git commit does not remove it from history.

## Reporting

Open a private GitHub security advisory or contact the repository owner. Do not include live credentials in an issue.
