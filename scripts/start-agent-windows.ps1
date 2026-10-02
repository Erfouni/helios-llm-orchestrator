param(
  [Parameter(Mandatory = $true)][string]$PythonBin,
  [string]$CredentialPath = "$env:APPDATA\Helios\openrouter-key.dpapi",
  [switch]$Log
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$keyWasLoaded = $false

if (-not $env:OPENROUTER_API_KEY) {
  if (-not (Test-Path -LiteralPath $CredentialPath)) {
    throw "OpenRouter credential is missing. Run scripts\configure-key-windows.ps1."
  }
  $secure = Get-Content -LiteralPath $CredentialPath -Raw | ConvertTo-SecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    $env:OPENROUTER_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $keyWasLoaded = $true
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  }
}

try {
  if ($Log) {
    $logDirectory = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $stdoutLog = Join-Path $logDirectory "stdout.log"
    $stderrLog = Join-Path $logDirectory "stderr.log"
    # Windows PowerShell turns each redirected stderr line of a native command
    # into an error record, and under "Stop" the first one (the first access-log
    # line) ends this script and takes the agent with it. Copy the lines as text.
    $ErrorActionPreference = "Continue"
    & $PythonBin (Join-Path $ProjectRoot "agent\server.py") 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) {
        [IO.File]::AppendAllText($stderrLog, $_.Exception.Message + [Environment]::NewLine)
      } else {
        [IO.File]::AppendAllText($stdoutLog, "$_" + [Environment]::NewLine)
      }
    }
    $ErrorActionPreference = "Stop"
  } else {
    & $PythonBin (Join-Path $ProjectRoot "agent\server.py")
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Helios agent exited with code $LASTEXITCODE."
  }
} finally {
  if ($keyWasLoaded) {
    Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue
  }
}
