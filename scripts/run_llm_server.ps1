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

function Resolve-ProjectFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ConfiguredPath,
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $candidate = if ([IO.Path]::IsPathRooted($ConfiguredPath)) {
        $ConfiguredPath
    }
    else {
        Join-Path $ProjectRoot $ConfiguredPath
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "$Description does not exist: $candidate"
    }
    return (Resolve-Path -LiteralPath $candidate).Path
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

    $serverConfigured = Get-ConfiguredValue "LLAMA_SERVER_BIN" "tools/llama.cpp/llama-server.exe"
    $modelConfigured = Get-ConfiguredValue "LLAMA_MODEL_PATH" "models/text/Qwen3-4B-Q4_K_M.gguf"
    $serverHost = Get-ConfiguredValue "LLAMA_SERVER_HOST" "127.0.0.1"
    $modelId = Get-ConfiguredValue "LLAMA_MODEL_ID" "Qwen3-4B-Q4_K_M.gguf"
    $serverPort = ConvertTo-RangedInt "LLAMA_SERVER_PORT" (Get-ConfiguredValue "LLAMA_SERVER_PORT" "8081") 1 65535
    $contextSize = ConvertTo-RangedInt "LLAMA_CONTEXT_SIZE" (Get-ConfiguredValue "LLAMA_CONTEXT_SIZE" "8192") 512 1048576
    $gpuLayers = ConvertTo-RangedInt "LLAMA_GPU_LAYERS" (Get-ConfiguredValue "LLAMA_GPU_LAYERS" "99") 0 10000

    if ($serverHost -ne "127.0.0.1") {
        throw "LLAMA_SERVER_HOST must remain 127.0.0.1; LAN exposure is not allowed in M3."
    }

    $serverPath = Resolve-ProjectFile $serverConfigured "llama-server executable" $projectRoot
    $modelPath = Resolve-ProjectFile $modelConfigured "GGUF model" $projectRoot
    if ([IO.Path]::GetExtension($serverPath) -ne ".exe") {
        throw "LLAMA_SERVER_BIN must point to a Windows .exe file: $serverPath"
    }
    if ([IO.Path]::GetExtension($modelPath) -ne ".gguf") {
        throw "LLAMA_MODEL_PATH must point to a .gguf file: $modelPath"
    }

    $serverArguments = @(
        "--model", $modelPath,
        "--alias", $modelId,
        "--host", $serverHost,
        "--port", $serverPort.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--ctx-size", $contextSize.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--n-gpu-layers", $gpuLayers.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--parallel", "1",
        "--flash-attn", "on",
        "--jinja",
        "--reasoning", "off",
        "--metrics",
        "--cors-origins", "localhost",
        "--no-cors-credentials",
        "--no-webui"
    )

    $configuration = [ordered]@{
        project_root = $projectRoot
        server_bin = $serverPath
        model_path = $modelPath
        model_id = $modelId
        listen_url = "http://${serverHost}:$serverPort"
        context_size = $contextSize
        gpu_layers = $gpuLayers
        reasoning = "off"
        parallel_slots = 1
        api_health = "http://${serverHost}:$serverPort/health"
        api_models = "http://${serverHost}:$serverPort/v1/models"
    }
    Write-Host "Starting llama.cpp in the foreground. Press Ctrl+C once to stop it."
    Write-Host ($configuration | ConvertTo-Json -Depth 3)

    # Direct invocation keeps the native process attached to this console, so
    # Ctrl+C is delivered to llama-server instead of a detached child process.
    & $serverPath @serverArguments
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -notin @(0, 130, -1073741510)) {
        throw "llama-server exited with code $serverExitCode. Review its output above."
    }
}
catch {
    Write-Error "Unable to start the local LLM server: $($_.Exception.Message)"
    exit 1
}
