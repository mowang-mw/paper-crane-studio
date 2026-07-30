param(
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot

$pythonCommand = Get-Command python -ErrorAction Stop
Write-Host "Python: $($pythonCommand.Source)"
Write-Host "Backend: http://${HostAddress}:$Port"

& $pythonCommand.Source -m uvicorn backend.app.main:app --host $HostAddress --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Backend exited with code $LASTEXITCODE. Confirm that the anime-platform Conda environment is active."
}
