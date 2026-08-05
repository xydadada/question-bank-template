$Root = Split-Path -Parent $PSScriptRoot
$Cli = Join-Path $Root "bin\weknora.exe"
if (Test-Path $Cli) {
    & $Cli doctor --format json --profile local
} else {
    Write-Host "WeKnora CLI: not built"
}
$PidFile = Join-Path $Root ".runtime\ingest.pid"
$State = "stopped"
if (Test-Path $PidFile) {
    $ProcessId = [int](Get-Content -Raw $PidFile)
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { $State = "running (PID $ProcessId)" }
}
Write-Host "ingest: $State"
& (Join-Path $Root "mcp-public\status.ps1")
