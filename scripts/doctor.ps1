$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Failures = [Collections.Generic.List[string]]::new()

function Check([bool]$Condition, [string]$Ok, [string]$Failure) {
    if ($Condition) { Write-Host "[OK] $Ok" } else { Write-Host "[FAIL] $Failure"; $Failures.Add($Failure) }
}

foreach ($Name in "git", "go", "uv", "wsl.exe") {
    Check ([bool](Get-Command $Name -ErrorAction SilentlyContinue)) "$Name available" "$Name missing"
}
Check (Test-Path (Join-Path $Root ".env")) ".env exists" ".env missing; run bootstrap"
$Config = Join-Path $Root "config.local.yaml"
Check (Test-Path $Config) "config.local.yaml exists" "config.local.yaml missing; run bootstrap"
if (Test-Path $Config) {
    $Text = Get-Content -Raw $Config
    Check ($Text -notmatch '__[A-Z]+_KB_ID__') "knowledge-base IDs configured" "knowledge-base IDs still contain placeholders"
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $Python) {
    & $Python -m py_compile (Join-Path $Root "ingest.py")
    Check ($LASTEXITCODE -eq 0) "ingest.py parses" "ingest.py syntax check failed"
} else {
    $Failures.Add("Python environment missing; run bootstrap")
}

$Cli = Join-Path $Root "bin\weknora.exe"
if (Test-Path $Cli) {
    $RuntimeProfile = "local"
    if (Test-Path $Config) {
        $ProfileMatch = [regex]::Match((Get-Content -Raw -LiteralPath $Config), '(?m)^  profile:\s*["'']?([^\s#"'']+)')
        if ($ProfileMatch.Success) { $RuntimeProfile = $ProfileMatch.Groups[1].Value }
    }
    & $Cli doctor --format json --profile $RuntimeProfile
    Check ($LASTEXITCODE -eq 0) "WeKnora CLI doctor passed" "WeKnora CLI doctor failed"
} else {
    $Failures.Add("WeKnora CLI missing; run bootstrap")
}

$EnvText = if (Test-Path (Join-Path $Root ".env")) { Get-Content -Raw (Join-Path $Root ".env") } else { "" }
$HasMineru = $EnvText -match '(?m)^MINERU_API_TOKEN(?:_[A-Z0-9]+)?\s*=\s*[^\s#]+\s*$'
$HasMimo = $EnvText -match '(?m)^MIMO_API_KEY(?:_[0-9]+)?\s*=\s*[^\s#]+\s*$'
Check ([bool]$HasMineru) "at least one MinerU key is present (value hidden)" "no MinerU key detected"
if ($HasMimo) { Write-Host "[OK] at least one MiMo key is present (value hidden)" } else { Write-Host "[WARN] no MiMo key detected; image understanding will be unavailable" }

if ($Failures.Count) {
    throw "Doctor found $($Failures.Count) blocking problem(s)."
}
Write-Host "Doctor completed successfully."
