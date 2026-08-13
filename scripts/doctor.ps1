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
    Write-Host "[WARN] go missing; the existing WeKnora CLI can run, but bootstrap cannot rebuild it"
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
$HasMineru = $EnvText -match '(?m)^MINERU_API_TOKEN(?:_[A-Z0-9]+)?\s*=\s*[^\s#]+\s*$'
$HasMimo = $EnvText -match '(?m)^MIMO_API_KEY(?:_[0-9]+)?\s*=\s*[^\s#]+\s*$'
$ParserRuntime = "mineru-cloud"
$VisionRuntime = "openai-compatible"
$Selection = Join-Path $Root "models.local.yaml"
if ((Test-Path $Selection) -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    $ResolvedRaw = (& uv run python model_manager.py resolve 2>$null) -join "`n"
    if ($LASTEXITCODE -eq 0) {
        $Resolved = $ResolvedRaw | ConvertFrom-Json
        $ParserRuntime = [string]$Resolved.roles.parser.runtime
        $VisionRuntime = [string]$Resolved.roles.vision.runtime
        Write-Host "[OK] model preset resolved: $($Resolved.name)"
    } else {
        $Failures.Add("models.local.yaml could not be resolved")
    }
}
if ($ParserRuntime -eq "mineru-local") {
    $LocalMineru = @(
        (Join-Path $Root ".runtime\mineru\Scripts\mineru.exe"),
        (Join-Path $Root ".runtime\mineru\bin\mineru")
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($LocalMineru) {
        Write-Host "[OK] local MinerU runtime is installed"
    } else {
        Write-Host "[WARN] local MinerU is selected but not installed; run model_manager.py install"
    }
} elseif ($HasMineru) {
    Write-Host "[OK] at least one MinerU key is present (value hidden)"
} else {
    Write-Host "[WARN] cloud MinerU is selected without a key; existing knowledge-base retrieval can still work"
}
if ($VisionRuntime -eq "openai-compatible") {
    if ($HasMimo) { Write-Host "[OK] at least one MiMo key is present (value hidden)" } else { Write-Host "[WARN] MiMo vision is selected without a key" }
} else {
    Write-Host "[OK] vision role does not require a MiMo key"
}

if ($Failures.Count) {
    throw "Doctor found $($Failures.Count) blocking problem(s)."
}
Write-Host "Doctor completed successfully."
