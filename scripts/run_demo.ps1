param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDirectory = Join-Path $projectRoot "data\runtime"
$statePath = Join-Path $runtimeDirectory "demo-processes.json"
$condaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
if ($null -eq $condaCommand) {
    $condaCommand = Get-Command conda -ErrorAction Stop
}
$condaExecutable = $condaCommand.Source

function Test-RecordedProcess {
    param([object]$Service)
    $processId = [int]$Service.pid
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process -or [string]$Service.process_name -ne $process.ProcessName) {
        return $false
    }
    return [string]$Service.process_start_utc -eq $process.StartTime.ToUniversalTime().ToString("o")
}

function Test-LocalPortOpen {
    param([int]$Port)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(250)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Start-DemoService {
    param(
        [string]$Name,
        [string]$Title,
        [string]$ScriptPath,
        [string[]]$Arguments,
        [bool]$UseConda
    )

    $escapedRoot = $projectRoot.Replace("'", "''")
    $escapedScript = $ScriptPath.Replace("'", "''")
    $escapedConda = $condaExecutable.Replace("'", "''")
    $argumentText = ($Arguments | ForEach-Object {
        $argument = $_
        if ($argument -match "^-[A-Za-z][A-Za-z0-9]*$") {
            $argument
        }
        else {
            "'" + $argument.Replace("'", "''") + "'"
        }
    }) -join " "
    $serviceInvocation = if ($UseConda) {
        "& '$escapedConda' run --no-capture-output -n anime-platform powershell.exe " +
        "-NoProfile -ExecutionPolicy Bypass -File '$escapedScript' $argumentText"
    }
    else {
        "& '$escapedScript' $argumentText"
    }
    $childCommand = @"
`$Host.UI.RawUI.WindowTitle = '$Title'
Set-Location -LiteralPath '$escapedRoot'
$serviceInvocation
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-NoExit",
        "-EncodedCommand", $encoded
    ) -PassThru -WindowStyle Normal
    Write-Host "Started $Name in a separate window (PID $($process.Id))."
    return [ordered]@{
        name = $Name
        pid = $process.Id
        process_name = $process.ProcessName
        process_start_utc = $process.StartTime.ToUniversalTime().ToString("o")
        script = [IO.Path]::GetFileName($ScriptPath)
        conda_environment = $(if ($UseConda) { "anime-platform" } else { $null })
        started_at = [DateTimeOffset]::Now.ToString("o")
    }
}

$existing = @{}
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        foreach ($service in @($saved.services)) {
            $pidValue = [int]$service.pid
            if ($pidValue -gt 0 -and (Test-RecordedProcess $service)) {
                $existing[[string]$service.name] = $service
            }
        }
    }
    catch {
        Write-Warning "Ignoring stale demo process state: $($_.Exception.Message)"
    }
}

$definitions = @(
    [ordered]@{
        name = "Backend"
        title = "Anime Studio Demo - Backend"
        script = Join-Path $PSScriptRoot "run_backend.ps1"
        arguments = @("-Port", $BackendPort.ToString())
        use_conda = $true
        port = $BackendPort
    },
    [ordered]@{
        name = "Worker"
        title = "Anime Studio Demo - Worker"
        script = Join-Path $PSScriptRoot "run_worker.ps1"
        arguments = @()
        use_conda = $true
        port = $null
    },
    [ordered]@{
        name = "Frontend"
        title = "Anime Studio Demo - Frontend"
        script = Join-Path $PSScriptRoot "run_frontend.ps1"
        arguments = @("-Port", $FrontendPort.ToString())
        use_conda = $false
        port = $FrontendPort
    }
)

$active = @()
foreach ($definition in $definitions) {
    if ($existing.ContainsKey($definition.name)) {
        Write-Host "$($definition.name) is already owned by the demo launcher (PID $($existing[$definition.name].pid)); skipping."
        $active += $existing[$definition.name]
        continue
    }
    if ($null -ne $definition.port -and (Test-LocalPortOpen ([int]$definition.port))) {
        Write-Warning "$($definition.name) port $($definition.port) is already in use; no duplicate process was started."
        continue
    }
    $active += Start-DemoService `
        -Name $definition.name `
        -Title $definition.title `
        -ScriptPath $definition.script `
        -Arguments $definition.arguments `
        -UseConda $definition.use_conda
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
[ordered]@{
    project_root = $projectRoot
    services = @($active)
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Host "Demo infrastructure is ready. Backend, Worker, and Frontend logs remain visible in their own windows."
Write-Host "This launcher does not start model runtimes. Use .\scripts\stop_demo.ps1 to stop only processes owned by this launcher."
