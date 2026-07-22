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
    & $PythonBin @arguments 1>> (Join-Path $logDirectory "benchmark-refresh.stdout.log") 2>> (Join-Path $logDirectory "benchmark-refresh.stderr.log")
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
