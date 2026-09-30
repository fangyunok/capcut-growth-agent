param(
    [string]$Model = "qwen3:4b-instruct",
    [int]$Port = 7860
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $projectRoot ".venv")
    & $pythonExe -m pip install -e $projectRoot
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
}

$ollamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollamaCommand) {
    $ollamaExe = $ollamaCommand.Source
} else {
    $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $ollamaExe = Get-ChildItem -LiteralPath $packageRoot -Directory -Filter "Ollama.Ollama.Portable_*" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "ollama.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
}
if (-not $ollamaExe) {
    throw "Ollama is missing. Install it from https://ollama.com/download/windows and rerun this script."
}

function Get-OllamaTags {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
    } catch {
        return $null
    }
}

$tags = Get-OllamaTags
if (-not $tags) {
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        $tags = Get-OllamaTags
        if ($tags) { break }
    }
    if (-not $tags) { throw "Ollama did not start on 127.0.0.1:11434." }
}

$installedModels = @($tags.models | ForEach-Object { $_.name })
if ($Model -notin $installedModels) {
    Write-Host "Downloading $Model through Ollama. This may take several minutes."
    & $ollamaExe pull $Model
    if ($LASTEXITCODE -ne 0) { throw "Ollama model download failed." }
}

$env:GROWTH_QWEN_MODEL = $Model
Write-Host "Growth Agent: http://127.0.0.1:$Port"
& $pythonExe -m growth_agent serve --host 127.0.0.1 --port $Port
