[CmdletBinding()]
param(
    [string]$HostUrl = "http://127.0.0.1:8080",
    [string]$Profile = "local",
    [string]$EmbeddingModel = "qwen3-embedding:0.6b",
    [int]$EmbeddingDimension = 1024,
    [string]$OllamaBaseUrl = "http://host.docker.internal:11434"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Cli = Join-Path $Root "bin\weknora.exe"
$Config = Join-Path $Root "config.local.yaml"
if (-not (Test-Path $Cli)) { throw "Missing bin/weknora.exe. Run scripts/bootstrap.ps1 first." }
if (-not (Test-Path $Config)) { throw "Missing config.local.yaml. Run scripts/bootstrap.ps1 first." }

function Run-Json([string[]]$Arguments) {
    $Raw = (& $Cli @Arguments 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw $Raw }
    return $Raw | ConvertFrom-Json
}

$Profiles = Run-Json @("profile", "list", "--format", "json")
$ProfileRows = @($Profiles.data)
if (-not ($ProfileRows | Where-Object { $_.name -eq $Profile })) {
    Run-Json @("profile", "add", $Profile, "--host", $HostUrl, "--use", "--format", "json") | Out-Null
} else {
    Run-Json @("profile", "use", $Profile, "--format", "json") | Out-Null
}

Write-Host "Sign in to your local WeKnora account. Password input is handled by the official CLI and is not written by this script."
& $Cli auth login --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "WeKnora login failed." }

if (Get-Command ollama -ErrorAction SilentlyContinue) {
    & ollama pull $EmbeddingModel
    if ($LASTEXITCODE -ne 0) { throw "Ollama model pull failed." }
} else {
    Write-Warning "Ollama is not in PATH. Ensure '$EmbeddingModel' is available before indexing."
}

$Models = Run-Json @("model", "list", "--format", "json", "--profile", $Profile)
$ModelRows = @($Models.data)
if (-not ($ModelRows | Where-Object { $_.name -eq $EmbeddingModel -and $_.type -eq "Embedding" })) {
    Run-Json @(
        "model", "create", $EmbeddingModel,
        "--source", "local", "--type", "Embedding",
        "--dimension", "$EmbeddingDimension",
        "--base-url", $OllamaBaseUrl,
        "--display-name", "Local $EmbeddingModel",
        "--format", "json", "--profile", $Profile
    ) | Out-Null
}

function Ensure-KnowledgeBase([string]$Name, [string]$Description) {
    $List = Run-Json @("kb", "list", "--format", "json", "--profile", $Profile)
    $Existing = @($List.data) | Where-Object { $_.name -eq $Name } | Select-Object -First 1
    if ($Existing) { return [string]$Existing.id }
    $Created = Run-Json @(
        "kb", "create", $Name,
        "--description", $Description,
        "--embedding-model", $EmbeddingModel,
        "--format", "json", "--profile", $Profile
    )
    return [string]$Created.data.id
}

$ParentId = Ensure-KnowledgeBase "Question bank - parent" "Complete question-and-answer groups"
$ChildId = Ensure-KnowledgeBase "Question bank - child" "Fine-grained question chunks"
$RawId = Ensure-KnowledgeBase "Question bank - raw" "Full source Markdown"

$Text = Get-Content -Raw -LiteralPath $Config
$Text = $Text.Replace('"__PARENT_KB_ID__"', $ParentId)
$Text = $Text.Replace('"__CHILD_KB_ID__"', $ChildId)
$Text = $Text.Replace('"__RAW_KB_ID__"', $RawId)
$Text = [regex]::Replace($Text, '(?m)^  profile: .+$', "  profile: $Profile")
$Text = [regex]::Replace($Text, '(?m)^  setup_profile: .+$', "  setup_profile: $Profile")
[System.IO.File]::WriteAllText($Config, $Text, [System.Text.UTF8Encoding]::new($false))

& $Cli doctor --format json --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "WeKnora doctor failed." }
Write-Host "WeKnora configured. Knowledge-base IDs were written only to ignored config.local.yaml."
