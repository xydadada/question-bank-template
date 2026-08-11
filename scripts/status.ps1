[CmdletBinding()]
param([string]$ExternalUrl = $env:MCP_EXTERNAL_URL)

$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root ".runtime"
$Cli = Join-Path $Root "bin\weknora.exe"
$RuntimeProfile = "local"
$LocalConfig = Join-Path $Root "config.local.yaml"
if (Test-Path $LocalConfig) {
    $ConfigText = [IO.File]::ReadAllText($LocalConfig, [Text.Encoding]::UTF8)
    $ProfileMatch = [regex]::Match($ConfigText, '(?m)^  profile:\s*["'']?([^\s#"'']+)')
    if ($ProfileMatch.Success) { $RuntimeProfile = $ProfileMatch.Groups[1].Value }
}

function Show-HttpStatus([string]$Name, [string]$Uri) {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5
        Write-Host "$Name`: HTTP $($Response.StatusCode)"
    } catch {
        $Code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { "unavailable" }
        Write-Host "$Name`: $Code"
    }
}

Show-HttpStatus "WeKnora API" "http://127.0.0.1:8080/health"
Show-HttpStatus "WeKnora UI" "http://127.0.0.1:8088"
Show-HttpStatus "Ollama" "http://127.0.0.1:11434/api/tags"

if (Test-Path $Cli) {
    & $Cli doctor --format json --profile $RuntimeProfile
    if ($LASTEXITCODE -ne 0) { Write-Warning "CLI doctor did not pass; inspect the JSON above." }
} else {
    Write-Host "WeKnora CLI: not built"
}

$PidFile = Join-Path $Runtime "ingest.pid"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$IngestScript = Join-Path $Root "ingest.py"
$Pattern = '(?i)' + [regex]::Escape($IngestScript) + '.*--supervise'
$SavedPid = 0
if (Test-Path $PidFile) {
    try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
}
$Managed = @()
if (Test-Path $Python) {
    $Expected = [IO.Path]::GetFullPath($Python)
    $Managed = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals($Expected, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match $Pattern
    })
}
$State = if ($Managed.Count -gt 1) {
    "ERROR: duplicate managed processes (PIDs $(@($Managed.ProcessId) -join ', '))"
} elseif ($Managed.Count -eq 1) {
    $Suffix = if ($SavedPid -eq $Managed[0].ProcessId) { "verified PID file" } else { "PID file missing or stale" }
    "running (PID $($Managed[0].ProcessId), $Suffix)"
} elseif (Test-Path $PidFile) {
    "stopped (stale PID file)"
} else {
    "stopped"
}
Write-Host "ingest: $State"
& (Join-Path $Root "mcp-public\status.ps1") -ExternalUrl $ExternalUrl
