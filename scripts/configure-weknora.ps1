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
$ExistingProfile = $ProfileRows | Where-Object { $_.name -eq $Profile } | Select-Object -First 1
if (-not $ExistingProfile) {
    Run-Json @("profile", "add", $Profile, "--host", $HostUrl, "--use", "--format", "json") | Out-Null
} else {
    if (-not ([string]$ExistingProfile.host).TrimEnd('/').Equals(
        $HostUrl.TrimEnd('/'), [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Profile '$Profile' points to '$($ExistingProfile.host)', expected '$HostUrl'. Remove or rename that profile explicitly before rerunning."
    }
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

$Models = Run-Json @("model", "list", "--limit", "10000", "--format", "json", "--profile", $Profile)
$ModelRows = @($Models.data)
$ExistingModel = $ModelRows | Where-Object {
    $_.name -eq $EmbeddingModel -and $_.type -eq "Embedding"
} | Select-Object -First 1
if (-not $ExistingModel) {
    $CreatedModel = Run-Json @(
        "model", "create", $EmbeddingModel,
        "--source", "local", "--type", "Embedding",
        "--dimension", "$EmbeddingDimension",
        "--base-url", $OllamaBaseUrl,
        "--display-name", "Local $EmbeddingModel",
        "--format", "json", "--profile", $Profile
    )
    $EmbeddingModelId = [string]$CreatedModel.data.id
} else {
    $ModelView = Run-Json @(
        "model", "view", ([string]$ExistingModel.id),
        "--format", "json", "--profile", $Profile
    )
    $ModelData = $ModelView.data
    $ActualDimension = [int]$ModelData.parameters.embedding_parameters.dimension
    $ActualBaseUrl = ([string]$ModelData.parameters.base_url).TrimEnd('/')
    $ExpectedBaseUrl = $OllamaBaseUrl.TrimEnd('/')
    if (
        $ModelData.source -ne "local" -or
        $ActualDimension -ne $EmbeddingDimension -or
        -not $ActualBaseUrl.Equals($ExpectedBaseUrl, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Existing embedding model '$EmbeddingModel' conflicts with the requested local configuration. Expected source=local, dimension=$EmbeddingDimension, base-url=$ExpectedBaseUrl; found source=$($ModelData.source), dimension=$ActualDimension, base-url=$ActualBaseUrl. Update or remove that model explicitly before rerunning."
    }
    $EmbeddingModelId = [string]$ExistingModel.id
}
if (-not $EmbeddingModelId) { throw "Embedding model ID could not be determined." }

function Ensure-KnowledgeBase([string]$Name, [string]$Description) {
    $List = Run-Json @("kb", "list", "--limit", "10000", "--format", "json", "--profile", $Profile)
    $Matches = @($List.data) | Where-Object { $_.name -eq $Name }
    if ($Matches.Count -gt 1) {
        throw "More than one knowledge base is named '$Name'. Rename or remove the duplicate explicitly before rerunning."
    }
    $Existing = $Matches | Select-Object -First 1
    if ($Existing) {
        if ([string]$Existing.embedding_model_id -ne $EmbeddingModelId) {
            throw "Knowledge base '$Name' uses embedding model '$($Existing.embedding_model_id)', expected '$EmbeddingModelId'. Change it explicitly or use a different knowledge-base name."
        }
        return [string]$Existing.id
    }
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

function Set-YamlScalar([string]$Yaml, [string]$Name, [string]$Value) {
    $Pattern = [regex]::new(
        "(?m)^(?<prefix>\s*" + [regex]::Escape($Name) + "\s*:\s*).*$"
    )
    $Matches = $Pattern.Matches($Yaml)
    if ($Matches.Count -ne 1) {
        throw "Expected exactly one '$Name' setting in config.local.yaml; found $($Matches.Count)."
    }
    $Escaped = $Value.Replace('"', '\"')
    return $Pattern.Replace(
        $Yaml,
        [Text.RegularExpressions.MatchEvaluator]{
            param($Match)
            $Match.Groups['prefix'].Value + '"' + $Escaped + '"'
        },
        1
    )
}

$Text = [IO.File]::ReadAllText($Config, [Text.Encoding]::UTF8)
$Text = Set-YamlScalar $Text "knowledge_base" $ParentId
$Text = Set-YamlScalar $Text "parent_knowledge_base" $ParentId
$Text = Set-YamlScalar $Text "child_knowledge_base" $ChildId
$Text = Set-YamlScalar $Text "raw_knowledge_base" $RawId
$Text = [regex]::Replace($Text, '(?m)^  profile: .+$', "  profile: $Profile")
$Text = [regex]::Replace($Text, '(?m)^  setup_profile: .+$', "  setup_profile: $Profile")
[System.IO.File]::WriteAllText($Config, $Text, [System.Text.UTF8Encoding]::new($false))

& $Cli doctor --format json --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "WeKnora doctor failed." }
Write-Host "WeKnora configured. Knowledge-base IDs were written only to ignored config.local.yaml."
