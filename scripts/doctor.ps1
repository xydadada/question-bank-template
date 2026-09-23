$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Failures = [Collections.Generic.List[string]]::new()

function Check([bool]$Condition, [string]$Ok, [string]$Failure) {
    if ($Condition) { Write-Host "[OK] $Ok" } else { Write-Host "[FAIL] $Failure"; $Failures.Add($Failure) }
}

foreach ($Name in "git", "uv", "wsl.exe") {
    Check ([bool](Get-Command $Name -ErrorAction SilentlyContinue)) "$Name available" "$Name missing"
}
$Cli = Join-Path $Root "bin\weknora.exe"
$HasGo = [bool](Get-Command "go" -ErrorAction SilentlyContinue)
if ($HasGo) {
    Write-Host "[OK] go available"
} elseif (Test-Path $Cli) {
    Write-Host "[OK] existing WeKnora CLI can run; Go is only needed to rebuild it"
} else {
    $Failures.Add("go missing and WeKnora CLI has not been built; run bootstrap after installing Go")
}
Check (Test-Path (Join-Path $Root ".env")) ".env exists" ".env missing; run bootstrap"
$Config = Join-Path $Root "config.local.yaml"
Check (Test-Path $Config) "config.local.yaml exists" "config.local.yaml missing; run bootstrap"
if (Test-Path $Config) {
    $Text = [IO.File]::ReadAllText($Config, [Text.Encoding]::UTF8)
    Check ($Text -notmatch '__[A-Z]+_KB_ID__') "knowledge-base IDs configured" "knowledge-base IDs still contain placeholders"
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $Python) {
    & $Python -m py_compile (Join-Path $Root "ingest.py")
    Check ($LASTEXITCODE -eq 0) "ingest.py parses" "ingest.py syntax check failed"
} else {
    $Failures.Add("Python environment missing; run bootstrap")
}

if (Test-Path $Cli) {
    $RuntimeProfile = "local"
    if (Test-Path $Config) {
        $ConfigText = [IO.File]::ReadAllText($Config, [Text.Encoding]::UTF8)
        $ProfileMatch = [regex]::Match($ConfigText, '(?m)^\s+profile:\s*["'']?([^\s#"'']+)')
        if ($ProfileMatch.Success) { $RuntimeProfile = $ProfileMatch.Groups[1].Value }
    }
    & $Cli doctor --format json --profile $RuntimeProfile
    Check ($LASTEXITCODE -eq 0) "WeKnora CLI doctor passed" "WeKnora CLI doctor failed"
} else {
    $Failures.Add("WeKnora CLI missing; run bootstrap")
}

$EnvPath = Join-Path $Root ".env"
$EnvText = if (Test-Path $EnvPath) { [IO.File]::ReadAllText($EnvPath, [Text.Encoding]::UTF8) } else { "" }
$MineruText = $EnvText
$MimoText = $EnvText
foreach ($Entry in @(@('mineru-keys.env', 'MineruText'), @('mimo-keys.env', 'MimoText'))) {
    $PrivatePath = Join-Path $Root $Entry[0]
    if (Test-Path -LiteralPath $PrivatePath) {
        if ($Entry[1] -eq 'MineruText') { $MineruText += "`n" + [IO.File]::ReadAllText($PrivatePath, [Text.Encoding]::UTF8) }
        else { $MimoText += "`n" + [IO.File]::ReadAllText($PrivatePath, [Text.Encoding]::UTF8) }
    }
}
$HasMineru = $MineruText -match '(?m)^MINERU_API_TOKEN(?:_[A-Z0-9]+)?\s*=\s*[^\s#]+\s*$'
$HasMimo = $MimoText -match '(?m)^MIMO_API_KEY(?:_[0-9]+)?\s*=\s*[^\s#]+\s*$'
if ($HasMineru) { Write-Host "[OK] at least one MinerU key is present (value hidden)" } else { Write-Host "[WARN] no MinerU key detected; ingestion is unavailable, but existing knowledge-base retrieval can still work" }
if ($HasMimo) {
    Write-Host "[OK] at least one MiMo key is present (value hidden)"
} elseif ($ConfigText -match '(?m)^    enabled:\s*false\s*$' -and $ConfigText -match '(?m)^    fallback_to_ollama:\s*true\s*$') {
    Write-Host "[OK] local Ollama image understanding selected; no MiMo key needed"
} else {
    Write-Host "[WARN] no MiMo key detected; image understanding will be unavailable"
}

if ($Failures.Count) {
    throw "Doctor found $($Failures.Count) blocking problem(s)."
}
Write-Host "Doctor completed successfully."
