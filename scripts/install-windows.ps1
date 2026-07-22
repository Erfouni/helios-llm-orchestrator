param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AgentTaskName = "Helios LLM Orchestrator"
$BenchmarkTaskName = "Helios Weekly Benchmark Refresh"

function Invoke-Native {
  param([string]$FilePath, [string[]]$Arguments)
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FilePath failed with exit code $LASTEXITCODE."
  }
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
$PythonBin = if ($PythonCommand) { $PythonCommand.Source } else { $null }
if (-not $PythonBin) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($launcher) {
    $PythonBin = (& $launcher.Source -3 -c "import sys; print(sys.executable)").Trim()
  }
}
$NodeBin = (Get-Command node -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$NpmBin = (Get-Command npm.cmd -ErrorAction SilentlyContinue | Select-Object -First 1).Source

if (-not $PythonBin) { throw "Python 3.10+ is required." }
if (-not $NodeBin) { throw "Node.js 22+ is required." }
if (-not $NpmBin) { throw "npm is required." }

$PythonVersion = [version]((& $PythonBin -c "import platform; print(platform.python_version())").Trim())
$NodeVersion = [version]((& $NodeBin -p "process.versions.node").Trim())
if ($PythonVersion -lt [version]"3.10") { throw "Python 3.10+ is required; found $PythonVersion." }
if ($NodeVersion.Major -lt 22) { throw "Node.js 22+ is required; found $NodeVersion." }

Push-Location $ProjectRoot
try {
  $env:HELIOS_PYTHON_BIN = $PythonBin
  Invoke-Native $NpmBin @("ci")
  Invoke-Native $NpmBin @("test")
  Invoke-Native $NpmBin @("run", "scan:secrets")
  Invoke-Native $NpmBin @("run", "audit:prod")
} finally {
  Remove-Item Env:HELIOS_PYTHON_BIN -ErrorAction SilentlyContinue
  Pop-Location
}

$CredentialPath = "$env:APPDATA\Helios\openrouter-key.dpapi"
if (-not (Test-Path -LiteralPath $CredentialPath)) {
  & (Join-Path $PSScriptRoot "configure-key-windows.ps1") -CredentialPath $CredentialPath
}

$PowerShell = (Get-Command powershell.exe).Source
$AgentScript = Join-Path $PSScriptRoot "start-agent-windows.ps1"
$RefreshScript = Join-Path $PSScriptRoot "refresh-benchmarks-windows.ps1"
$quote = [char]34
$AgentArguments = "-NoProfile -ExecutionPolicy Bypass -File $quote$AgentScript$quote -PythonBin $quote$PythonBin$quote -Log"
$RefreshArguments = "-NoProfile -ExecutionPolicy Bypass -File $quote$RefreshScript$quote -PythonBin $quote$PythonBin$quote -Log"

$principalId = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $principalId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$agentAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $AgentArguments -WorkingDirectory $ProjectRoot
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $principalId
Register-ScheduledTask -TaskName $AgentTaskName -Action $agentAction -Trigger $agentTrigger -Principal $principal -Settings $settings -Force | Out-Null

$refreshAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $RefreshArguments -WorkingDirectory $ProjectRoot
$refreshTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3:00AM
Register-ScheduledTask -TaskName $BenchmarkTaskName -Action $refreshAction -Trigger $refreshTrigger -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $AgentTaskName
try {
  & $RefreshScript -PythonBin $PythonBin
} catch {
  Write-Warning "Initial benchmark refresh failed; the weekly task will retry. $($_.Exception.Message)"
}

Write-Host "Helios installed. Health: http://127.0.0.1:3188/health"
Write-Host "Weekly public benchmark refresh scheduled for Monday at 03:00 local time."
