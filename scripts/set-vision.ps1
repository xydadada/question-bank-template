[CmdletBinding()]
param(
    [ValidateSet('mimo', 'ollama')][string]$Provider = 'mimo',
    [ValidateSet('qwen3.5:0.8b', 'qwen3.5:2b')][string]$VisionModel = 'qwen3.5:0.8b'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Config = Join-Path $Root 'config.local.yaml'
if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath (Join-Path $Root 'config.example.yaml') -Destination $Config
}

if ($Provider -eq 'ollama') {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        throw 'Ollama is required for local image understanding. Install/start it and retry.'
    }
    & ollama pull $VisionModel
    if ($LASTEXITCODE -ne 0) { throw "Failed to download $VisionModel." }
    & ollama pull 'qwen3:0.6b'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to download the local classification model.' }
}

function Set-Scalar([string]$Yaml, [string]$Pattern, [string]$Value, [string]$Name) {
    $Matches = [regex]::Matches($Yaml, $Pattern)
    if ($Matches.Count -ne 1) { throw "Expected one $Name setting, found $($Matches.Count)." }
    return ([regex]::new($Pattern)).Replace($Yaml, [Text.RegularExpressions.MatchEvaluator]{
        param($Match)
        $Match.Groups[1].Value + ' ' + $Value
    }, 1)
}

$Text = [IO.File]::ReadAllText($Config, [Text.Encoding]::UTF8)
$Enabled = if ($Provider -eq 'mimo') { 'true' } else { 'false' }
$Fallback = if ($Provider -eq 'ollama') { 'true' } else { 'false' }
$Text = Set-Scalar $Text '(?m)^(  vision_model:).*$' $VisionModel 'vision_model'
$Text = Set-Scalar $Text '(?m)^(    enabled:).*$' $Enabled 'mimo.enabled'
$Text = Set-Scalar $Text '(?m)^(    fallback_to_ollama:).*$' $Fallback 'mimo.fallback_to_ollama'
$Text = Set-Scalar $Text '(?m)^(    classification_fallback_to_ollama:).*$' $Fallback 'mimo.classification_fallback_to_ollama'
[IO.File]::WriteAllText($Config, $Text, [Text.UTF8Encoding]::new($false))
Write-Host "Image understanding provider configured: $Provider"
