param(
  [string]$CredentialPath = "$env:APPDATA\Helios\openrouter-key.dpapi"
)

$ErrorActionPreference = "Stop"
$directory = Split-Path -Parent $CredentialPath
New-Item -ItemType Directory -Force -Path $directory | Out-Null

$secret = Read-Host "OpenRouter API key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
  if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Length -lt 20) {
    throw "The OpenRouter API key appears to be empty or invalid."
  }
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}

$encrypted = ConvertFrom-SecureString -SecureString $secret
Set-Content -LiteralPath $CredentialPath -Value $encrypted -Encoding UTF8 -NoNewline
Write-Host "OpenRouter key encrypted with Windows DPAPI for the current user."
