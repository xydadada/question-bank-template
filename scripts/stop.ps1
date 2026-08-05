[CmdletBinding()]
param([string]$WslDistro = "Ubuntu", [switch]$StopWeKnora)

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root ".runtime\ingest.pid"
if (Test-Path $PidFile) {
    $ProcessId = [int](Get-Content -Raw $PidFile)
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { Stop-Process -Id $ProcessId }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}
& (Join-Path $Root "mcp-public\stop.ps1")
if ($StopWeKnora) {
    $WeKnora = Join-Path $Root ".runtime\WeKnora"
    if (Test-Path $WeKnora) {
        $WslPath = ((& wsl.exe -d $WslDistro -- wslpath -a $WeKnora) -join "").Trim()
        if ($WslPath.Contains("'")) { throw "Repository path cannot contain a single quote." }
        & wsl.exe -d $WslDistro -- bash -lc "cd '$WslPath' && docker compose stop"
    }
}
Write-Host "Local workers stopped. WeKnoraStopped=$StopWeKnora"
