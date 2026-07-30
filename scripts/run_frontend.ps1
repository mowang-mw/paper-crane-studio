param(
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 5173,
    [string]$ApiBaseUrl = "http://127.0.0.1:8000/api"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendRoot = Join-Path $projectRoot "frontend"

if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "package.json"))) {
    throw "frontend/package.json was not found."
}

$npmCommand = Get-Command npm -ErrorAction Stop
$env:VITE_API_BASE_URL = $ApiBaseUrl
Set-Location -LiteralPath $frontendRoot

Write-Host "Frontend: http://${HostAddress}:$Port"
Write-Host "API base: $ApiBaseUrl"

& $npmCommand.Source run dev -- --host $HostAddress --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Frontend exited with code $LASTEXITCODE."
}
