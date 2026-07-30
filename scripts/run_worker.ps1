param(
    [switch]$Once,
    [ValidateRange(0.1, 60.0)]
    [double]$PollInterval = 1.0
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot

$pythonCommand = Get-Command python -ErrorAction Stop
$workerArguments = @("-m", "backend.app.worker", "--poll-seconds", $PollInterval.ToString([Globalization.CultureInfo]::InvariantCulture))
if ($Once) {
    $workerArguments += "--once"
}

Write-Host "Python: $($pythonCommand.Source)"
Write-Host "Worker mode: $(if ($Once) { 'once' } else { 'continuous' })"

& $pythonCommand.Source @workerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Worker exited with code $LASTEXITCODE. Confirm that the anime-platform Conda environment is active."
}
