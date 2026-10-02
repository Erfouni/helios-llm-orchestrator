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
  $arguments = @((Join-Path $ProjectRoot "scripts\refresh-benchmarks.py"), "--if-stale")
  if ($Log) {
    $logDirectory = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $stdoutLog = Join-Path $logDirectory "benchmark-refresh.stdout.log"
    $stderrLog = Join-Path $logDirectory "benchmark-refresh.stderr.log"
    # Same as start-agent-windows.ps1: under "Stop", Windows PowerShell would end
    # the refresh at the first line Python writes to stderr (a warning is enough).
    $ErrorActionPreference = "Continue"
    & $PythonBin @arguments 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) {
        [IO.File]::AppendAllText($stderrLog, $_.Exception.Message + [Environment]::NewLine)
      } else {
        [IO.File]::AppendAllText($stdoutLog, "$_" + [Environment]::NewLine)
      }
    }
    $ErrorActionPreference = "Stop"
  } else {
    & $PythonBin @arguments
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Benchmark refresh exited with code $LASTEXITCODE."
  }
} finally {
  if ($keyWasLoaded) {
    Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue
  }
}
