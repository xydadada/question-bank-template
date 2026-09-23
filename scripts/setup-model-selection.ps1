[CmdletBinding()]
param(
    [ValidateSet('cloud', 'hybrid', 'local-light', 'local-balanced', 'local-quality')]
    [string]$Preset = 'cloud',
    [string]$Parser = '',
    [string]$Embedding = '',
    [int]$EmbeddingDimension = 0,
    [string]$Vision = '',
    [string]$Classification = '',
    [string]$CustomRole = '',
    [string]$CustomModel = '',
    [int]$CustomDimension = 0
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv before selecting models.' }
function Run-Manager([string[]]$Arguments) {
    & uv run python (Join-Path $Root 'model_manager.py') @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Model selection step failed: $($Arguments[0])" }
}
Push-Location $Root
try {
    Run-Manager @('select', $Preset)
    if ($Parser) { Run-Manager @('set', 'parser', $Parser) }
    if ($Embedding) {
        if ($EmbeddingDimension -le 0) { throw 'Embedding dimension is required.' }
        Run-Manager @('set', 'embedding', $Embedding, '--dimension', ([string]$EmbeddingDimension))
    }
    if ($Vision) { Run-Manager @('set', 'vision', $Vision) }
    if ($Classification) { Run-Manager @('set', 'classification', $Classification) }
    if ($CustomRole -or $CustomModel) {
        if ($CustomRole -notin @('ocr', 'vision', 'classification', 'embedding', 'chat') -or
            $CustomModel -notmatch '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$') {
            throw 'Choose a supported custom role and an Ollama model tag.'
        }
        $Args = @('use-ollama', $CustomRole, $CustomModel)
        if ($CustomRole -eq 'embedding') {
            if ($CustomDimension -le 0) { throw 'Custom Embedding needs its output dimension.' }
            $Args += @('--dimension', ([string]$CustomDimension))
        }
        Run-Manager $Args
    }
    Run-Manager @('resolve')
    Run-Manager @('install')
    Write-Host 'Selected model components are installed. Continue with WeKnora configuration.'
} finally {
    Pop-Location
}
