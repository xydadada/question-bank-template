$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($Name in "proxy", "cloudflared") {
    $PidFile = Join-Path $Base "$Name.pid"
    if (-not (Test-Path $PidFile)) { continue }
    $ProcessId = [int](Get-Content -Raw $PidFile)
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Process) { Stop-Process -Id $ProcessId }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}
Write-Host "MCP proxy and tunnel processes stopped."
