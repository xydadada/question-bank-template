[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Role,
    [Parameter(Mandatory = $true)][string]$Model,
    [int]$Dimension = 0
)

$ErrorActionPreference = 'Stop'
if ($Role -notin @('ocr', 'vision', 'classification', 'embedding', 'chat')) {
    throw 'Unsupported model role.'
}
if ($Model -notmatch '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$') {
    throw 'Invalid Ollama model tag.'
}
if ($Role -eq 'embedding' -and $Dimension -le 0) {
    throw 'Embedding needs its actual output dimension.'
}
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $Root 'models.local.yaml'))) {
    throw 'Select a base model combination first.'
}
Push-Location $Root
try {
    $Args = @('run', 'python', 'model_manager.py', 'use-ollama', $Role, $Model)
    if ($Role -eq 'embedding') { $Args += @('--dimension', ([string]$Dimension)) }
    & uv @Args
    if ($LASTEXITCODE -ne 0) { throw 'Could not select the custom Ollama model.' }
    & uv run python model_manager.py install
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the selected model.' }
} finally {
    Pop-Location
}
