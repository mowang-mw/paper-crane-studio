$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$statePath = Join-Path $projectRoot "data\runtime\demo-processes.json"

if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    Write-Host "No demo launcher state was found; nothing was stopped."
    exit 0
}

try {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}
catch {
    throw "Demo process state is unreadable; no process was stopped: $($_.Exception.Message)"
}

$remainingServices = @()
foreach ($service in @($state.services)) {
    $processId = [int]$service.pid
    if ($processId -le 0) {
        Write-Warning "Ignored invalid PID for $($service.name)."
        continue
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Host "$($service.name) PID $processId is no longer running."
        continue
    }
    $recordMatches = (
        [string]$service.process_name -eq $process.ProcessName -and
        [string]$service.process_start_utc -eq $process.StartTime.ToUniversalTime().ToString("o")
    )
    if (-not $recordMatches) {
        Write-Warning "Skipped $($service.name) PID $processId because its identity no longer matches launcher state."
        continue
    }
    & taskkill.exe /PID $processId /T /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not stop $($service.name) process tree (PID $processId)."
        $remainingServices += $service
    }
    else {
        Write-Host "Stopped $($service.name) process tree (PID $processId)."
    }
}

if ($remainingServices.Count -gt 0) {
    [ordered]@{
        project_root = $projectRoot
        services = @($remainingServices)
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Warning "Some owned process trees could not be stopped; launcher state was retained for a safe retry."
}
else {
    Remove-Item -LiteralPath $statePath -Force
    Write-Host "Demo launcher state removed. External services and model runtimes were not touched."
}
