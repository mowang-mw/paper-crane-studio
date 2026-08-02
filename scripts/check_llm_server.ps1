$ErrorActionPreference = "Stop"

function Get-ConfiguredValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Default
    )

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function ConvertTo-RangedInt {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value,
        [Parameter(Mandatory = $true)]
        [int]$Minimum,
        [Parameter(Mandatory = $true)]
        [int]$Maximum
    )

    $parsed = 0
    if (-not [int]::TryParse($Value, [ref]$parsed) -or $parsed -lt $Minimum -or $parsed -gt $Maximum) {
        throw "$Name must be an integer from $Minimum to $Maximum; actual value: $Value"
    }
    return $parsed
}

try {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $projectRoot

    $serverHost = Get-ConfiguredValue "LLAMA_SERVER_HOST" "127.0.0.1"
    $serverPort = ConvertTo-RangedInt "LLAMA_SERVER_PORT" (Get-ConfiguredValue "LLAMA_SERVER_PORT" "8081") 1 65535
    $timeoutSeconds = ConvertTo-RangedInt "LLAMA_CHECK_TIMEOUT_SECONDS" (Get-ConfiguredValue "LLAMA_CHECK_TIMEOUT_SECONDS" "10") 1 300
    $baseUrl = "http://${serverHost}:$serverPort"
    $healthUrl = "$baseUrl/health"
    $modelsUrl = "$baseUrl/v1/models"

    Write-Host "Checking local LLM server: $baseUrl"
    try {
        $health = Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec $timeoutSeconds
    }
    catch {
        throw "GET $healthUrl failed. Start scripts/run_llm_server.ps1 first. $($_.Exception.Message)"
    }
    if ($null -eq $health -or $health.status -ne "ok") {
        $actual = $health | ConvertTo-Json -Depth 5 -Compress
        throw "GET $healthUrl returned an unexpected payload: $actual"
    }

    try {
        $models = Invoke-RestMethod -Method Get -Uri $modelsUrl -TimeoutSec $timeoutSeconds
    }
    catch {
        throw "GET $modelsUrl failed. $($_.Exception.Message)"
    }
    $modelItems = @($models.data)
    if ($modelItems.Count -lt 1) {
        throw "GET $modelsUrl returned no model entries."
    }
    $modelIds = @($modelItems | ForEach-Object { $_.id } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($modelIds.Count -lt 1) {
        throw "GET $modelsUrl returned model entries without an id."
    }

    $summary = [ordered]@{
        status = "PASS"
        base_url = $baseUrl
        health_status = $health.status
        model_count = $modelItems.Count
        model_ids = $modelIds
    }
    Write-Host ($summary | ConvertTo-Json -Depth 5)
    exit 0
}
catch {
    Write-Error "Local LLM server check failed: $($_.Exception.Message)"
    exit 1
}
