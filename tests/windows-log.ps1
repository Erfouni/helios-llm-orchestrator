# Checks the -Log mode of the two scripts the Windows installer schedules, under
# Windows PowerShell 5.1 as the scheduled tasks run them: lines Python writes to
# stderr must end up in the log without stopping the script. Works on a copy of
# agent\ and scripts\, so a real logs\ folder is never touched.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows-log.ps1 -PythonBin <python.exe>
param(
  [Parameter(Mandatory = $true)][string]$PythonBin,
  [int]$Port = 3199
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path ([IO.Path]::GetTempPath()) ("helios-log-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root | Out-Null
foreach ($dir in "agent", "scripts") {
  Copy-Item -Recurse -Path (Join-Path $repo $dir) -Destination (Join-Path $root $dir)
}
$logs = Join-Path $root "logs"
$failures = New-Object System.Collections.Generic.List[string]
$env:OPENROUTER_API_KEY = "placeholder-not-a-real-key"
$env:OPENROUTER_AGENT_PORT = "$Port"

function Invoke-WindowsPowerShell([string]$Script, [string[]]$Arguments, [switch]$Wait) {
  $all = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Script`"") + $Arguments
  Start-Process -PassThru -Wait:$Wait -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList $all
}

$agent = $null
try {
  # The agent logs every request to stderr; it has to keep serving after that.
  $agent = Invoke-WindowsPowerShell (Join-Path $root "scripts\start-agent-windows.ps1") @("-PythonBin", "`"$PythonBin`"", "-Log")
  $listening = $false
  for ($i = 0; $i -lt 80 -and -not $listening; $i++) {
    Start-Sleep -Milliseconds 250
    try {
      $client = New-Object Net.Sockets.TcpClient("127.0.0.1", $Port)
      $client.Close()
      $listening = $true
    } catch {}
  }
  if (-not $listening) { $failures.Add("the agent never listened on port $Port") }
  for ($i = 1; $i -le 3 -and $listening; $i++) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "http://127.0.0.1:$Port/health"
      if ($response.StatusCode -ne 200) { $failures.Add("request $i got HTTP $($response.StatusCode)") }
    } catch {
      $failures.Add("request $i failed: $($_.Exception.Message)")
    }
  }
  if ($agent.HasExited) { $failures.Add("the agent script exited with code $($agent.ExitCode)") }
  $stderrLog = Join-Path $logs "stderr.log"
  $logged = @(Get-Content -ErrorAction SilentlyContinue $stderrLog | Where-Object { $_ -match '"GET /health HTTP/1\.1" 200' }).Count
  if ($logged -ne 3) { $failures.Add("stderr.log has $logged of the 3 access-log lines") }
  $stdoutLog = Join-Path $logs "stdout.log"
  if (-not (Select-String -Quiet -Pattern '"service": "helios-llm-orchestrator"' -Path $stdoutLog -ErrorAction SilentlyContinue)) {
    $failures.Add("stdout.log does not have the startup line")
  }

  # A refresh that writes a warning to stderr still has to finish and succeed.
  $fakePython = Join-Path $root "fake-python.cmd"
  Set-Content -Encoding ascii -Path $fakePython -Value @(
    "@echo off",
    "echo {""ok"": true}",
    "echo a warning on stderr 1>&2",
    "exit /b 0"
  )
  $refresh = Invoke-WindowsPowerShell (Join-Path $root "scripts\refresh-benchmarks-windows.ps1") @("-PythonBin", "`"$fakePython`"", "-Log") -Wait
  if ($refresh.ExitCode -ne 0) { $failures.Add("the refresh script exited with code $($refresh.ExitCode)") }
  if (-not (Select-String -Quiet -SimpleMatch -Pattern "a warning on stderr" -Path (Join-Path $logs "benchmark-refresh.stderr.log") -ErrorAction SilentlyContinue)) {
    $failures.Add("benchmark-refresh.stderr.log does not have the warning")
  }
  if (-not (Select-String -Quiet -SimpleMatch -Pattern '{"ok": true}' -Path (Join-Path $logs "benchmark-refresh.stdout.log") -ErrorAction SilentlyContinue)) {
    $failures.Add("benchmark-refresh.stdout.log does not have the result")
  }
} finally {
  if ($agent -and -not $agent.HasExited) { Stop-Process -Id $agent.Id -Force }
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*$root*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 500
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $root
  Remove-Item Env:OPENROUTER_API_KEY, Env:OPENROUTER_AGENT_PORT -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
  $failures | ForEach-Object { Write-Host "FAIL: $_" }
  exit 1
}
Write-Host "ok: the agent served 3 requests and logged them; the refresh finished with a stderr warning"
