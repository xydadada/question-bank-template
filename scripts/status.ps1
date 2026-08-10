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
$State = "stopped"
if (Test-Path $PidFile) {
    try { $SavedPid = [int](Get-Content -Raw -LiteralPath $PidFile) } catch { $SavedPid = 0 }
    $Row = if ($SavedPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction SilentlyContinue } else { $null }
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    if ($Row -and $Row.ExecutablePath -and (Test-Path $Python) -and
        [IO.Path]::GetFullPath($Row.ExecutablePath).Equals([IO.Path]::GetFullPath($Python), [StringComparison]::OrdinalIgnoreCase) -and
        $Row.CommandLine -match '(?i)ingest\.py.*--supervise') {
        $State = "running (PID $SavedPid)"
    } else {
        $State = "stale PID file"
    }
}
Write-Host "ingest: $State"
& (Join-Path $Root "mcp-public\status.ps1") -ExternalUrl $ExternalUrl
