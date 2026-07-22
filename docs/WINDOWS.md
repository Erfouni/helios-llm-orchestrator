# Windows installation and operation

Helios supports Windows 10 and Windows 11. The HTTP agent remains loopback-only at `127.0.0.1:3188`, and the MCP server still communicates with ChatGPT/Codex over stdio.

## Requirements

- Python 3.10 or newer
- Node.js 22 or newer
- npm and Git
- Windows PowerShell 5.1 or newer
- an OpenRouter API key

Confirm the runtimes:

```powershell
python --version
node --version
npm --version
git --version
```

## Install

```powershell
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
Set-Location helios-llm-orchestrator
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The installer:

1. validates Python and Node versions;
2. installs locked npm dependencies;
3. runs tests, secret scanning, and the production dependency audit;
4. prompts for the OpenRouter key with hidden input;
5. encrypts the key with Windows DPAPI under `%APPDATA%\Helios`;
6. registers `Helios LLM Orchestrator` to run at user logon;
7. registers `Helios Weekly Benchmark Refresh` for Monday at 03:00;
8. starts the agent and attempts an initial stale-only refresh.

The DPAPI credential can be decrypted only in the same Windows user context. Plaintext is provided only to the child Python process in memory and is not written to the repository.

## Verify

```powershell
Invoke-RestMethod http://127.0.0.1:3188/health
Get-ScheduledTask -TaskName "Helios LLM Orchestrator"
Get-ScheduledTask -TaskName "Helios Weekly Benchmark Refresh"
```

Logs are written under the repository's `logs` directory.

## MCP client configuration

Copy `mcp/client-config.windows.example.json` and replace the placeholder with an absolute Windows path:

```json
{
  "mcpServers": {
    "helios": {
      "command": "node",
      "args": [
        "C:\\ABSOLUTE\\PATH\\helios-llm-orchestrator\\mcp\\mcp-server.mjs"
      ],
      "env": {
        "HELIOS_AGENT_URL": "http://127.0.0.1:3188"
      }
    }
  }
}
```

Restart the MCP client after changing its configuration.

## Rotate the OpenRouter key

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure-key-windows.ps1
Stop-ScheduledTask -TaskName "Helios LLM Orchestrator" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "Helios LLM Orchestrator"
```

## Run checks manually

```powershell
npm ci
npm test
npm run scan:secrets
npm run audit:prod
```

## Remove the scheduled tasks

```powershell
Stop-ScheduledTask -TaskName "Helios LLM Orchestrator" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Helios LLM Orchestrator" -Confirm:$false
Unregister-ScheduledTask -TaskName "Helios Weekly Benchmark Refresh" -Confirm:$false
```

After unregistering the tasks, the repository and `%APPDATA%\Helios` credential directory can be removed manually if no longer needed.
